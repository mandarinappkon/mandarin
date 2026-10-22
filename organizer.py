"""
Mandarin — редактор страниц PDF («Упорядочить PDF», как на iLovePDF, но офлайн).

Миниатюры всех страниц из нескольких PDF и картинок. Перетаскивайте страницы мышью,
поворачивайте, удаляйте, дублируйте, вставляйте пустые, добавляйте файлы, отменяйте
действия — и сохраняйте результат в новый PDF. Оригиналы не изменяются.

Отдельный запуск:  python organizer.py файл1.pdf файл2.jpg …
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import engine
from ui_kit import (ACCENT, BG, CARD, DANGER, FILE_COLORS, IS_WIN, LINE, MUTED, PANEL, TEXT,
                    FlatButton, dnd_available, font, shortcut_key, tooltip, ui_scale)

_uid = itertools.count(1)


@dataclasses.dataclass
class Page:
    uid: int
    doc: int                 # индекс исходного документа (-1 для пустой)
    pno: int | None          # номер страницы в исходнике; None — пустая страница
    rot: int = 0             # дополнительный поворот по часовой: 0/90/180/270
    w: float = 595.0         # размер (для пустых страниц и пропорций)
    h: float = 842.0


@dataclasses.dataclass
class Source:
    path: Path
    doc: object              # pymupdf.Document
    color: str


class PdfOrganizer:
    MIN_ZOOM, MAX_ZOOM = 90, 280

    def __init__(self, master, files=(), on_done=None):
        self.k = ui_scale(master)
        self.win = tk.Toplevel(master)
        self.win.title("Mandarin — упорядочить PDF")
        self.win.configure(bg=BG)
        w, h = int(1180 * self.k), int(760 * self.k)
        self.win.geometry(f"{w}x{h}")
        self.win.minsize(int(760 * self.k), int(480 * self.k))
        self.on_done = on_done            # callback(title, body, path) для уведомлений

        self.sources: list[Source] = []
        self.pages: list[Page] = []
        self.sel: set[int] = set()
        self.anchor: int | None = None
        self.undo_stack: list[tuple] = []
        self.redo_stack: list[tuple] = []
        self.zoom = int(150 * self.k)
        self.thumbs: dict[tuple, object] = {}
        self.pending: dict[int, Page] = {}
        self.img_items: dict[int, int] = {}
        self.geom: dict[int, tuple] = {}  # uid → (x0, y0, x1, y1) карточки в координатах холста
        self.cols = 1
        self.hover_uid: int | None = None
        self.drag = None
        self.scroll_job = None
        self.dirty = False

        self._build()
        self._bind_keys()
        if files:
            self.win.after(30, lambda: self.add_files([Path(f) for f in files]))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.redraw()

    # ───────────────── интерфейс ─────────────────
    def _build(self):
        k = self.k
        top = tk.Frame(self.win, bg=PANEL, padx=int(10 * k), pady=int(8 * k))
        top.pack(side="top", fill="x")

        def btn(parent, text, cmd, tip=None, kind="normal"):
            b = FlatButton(parent, text, cmd, kind=kind, size=10, padx=int(10 * k), pady=int(6 * k))
            b.pack(side="left", padx=(0, int(6 * k)))
            if tip:
                tooltip(b, tip)
            return b

        btn(top, "＋ Добавить файлы", self.ask_add, "PDF и картинки (можно перетащить прямо в окно)", "accent")
        tk.Frame(top, bg=LINE, width=1, height=int(26 * k)).pack(side="left", padx=int(8 * k))
        btn(top, "⟲", lambda: self.rotate(-90), "Повернуть против часовой  (Shift+R)")
        btn(top, "⟳", lambda: self.rotate(90), "Повернуть по часовой  (R)")
        btn(top, "⧉ Дублировать", self.duplicate, "Копия выделенных страниц  (Ctrl+D)")
        btn(top, "▭ Пустая", self.insert_blank, "Вставить пустую страницу после выделения")
        btn(top, "🗑 Удалить", self.delete, "Удалить выделенные  (Delete)", "danger")
        tk.Frame(top, bg=LINE, width=1, height=int(26 * k)).pack(side="left", padx=int(8 * k))
        btn(top, "⇅ Обратный", self.reverse, "Обратный порядок (выделенных или всех)")
        btn(top, "↶", self.undo, "Отменить  (Ctrl+Z)")
        btn(top, "↷", self.redo, "Вернуть  (Ctrl+Y)")

        zf = tk.Frame(top, bg=PANEL)
        zf.pack(side="right")
        tk.Label(zf, text="Миниатюры", bg=PANEL, fg=MUTED, font=font(9)).pack(side="left", padx=(0, int(6 * k)))
        for txt, d in (("−", -1), ("+", 1)):
            b = FlatButton(zf, txt, lambda d=d: self.set_zoom(self.zoom + d * 30 * self.k), size=11,
                           padx=int(10 * k), pady=int(3 * k))
            b.pack(side="left", padx=(0, int(4 * k)))
            tooltip(b, "Размер миниатюр (Ctrl+колесо мыши)")

        body = tk.Frame(self.win, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        # правая панель
        side = tk.Frame(body, bg=PANEL, width=int(290 * k), padx=int(16 * k), pady=int(16 * k))
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="Упорядочить PDF", bg=PANEL, fg=TEXT, font=font(15, "bold"), anchor="w").pack(fill="x")
        tk.Label(side, text="Перетаскивайте страницы, чтобы поменять порядок. Ctrl/Shift+клик — выделить несколько.",
                 bg=PANEL, fg=MUTED, font=font(9), wraplength=int(255 * k), justify="left", anchor="w"
                 ).pack(fill="x", pady=(int(4 * k), int(12 * k)))
        tk.Label(side, text="ФАЙЛЫ", bg=PANEL, fg=MUTED, font=font(8, "bold"), anchor="w").pack(fill="x")
        self.files_box = tk.Frame(side, bg=PANEL)
        self.files_box.pack(fill="x", pady=(int(4 * k), int(12 * k)))

        tk.Label(side, text="СОРТИРОВКА", bg=PANEL, fg=MUTED, font=font(8, "bold"), anchor="w").pack(fill="x")
        sf = tk.Frame(side, bg=PANEL)
        sf.pack(fill="x", pady=(int(4 * k), int(12 * k)))
        FlatButton(sf, "По файлам", self.sort_by_file, size=9, padx=int(8 * k), pady=int(5 * k)).pack(side="left", padx=(0, int(6 * k)))
        FlatButton(sf, "Чередовать", self.interleave, size=9, padx=int(8 * k), pady=int(5 * k)).pack(side="left")
        tooltip(sf.winfo_children()[1], "1-я стр. файла А, 1-я стр. файла Б, 2-я А… (сканы лицевых/оборотных сторон)")

        # нижние кнопки панели
        bottom = tk.Frame(side, bg=PANEL)
        bottom.pack(side="bottom", fill="x")
        self.save_btn = FlatButton(bottom, "Сохранить PDF  →", self.save, kind="accent", size=12, pady=int(12 * k))
        self.save_btn.pack(fill="x", pady=(int(8 * k), 0))
        FlatButton(bottom, "Извлечь выделенные в PDF", self.extract_selected, size=9).pack(fill="x", pady=(int(6 * k), 0))
        FlatButton(bottom, "Каждую страницу — отдельным PDF", self.split_all, size=9).pack(fill="x", pady=(int(6 * k), 0))
        FlatButton(bottom, "Выделенные — в картинки PNG", self.export_png, size=9).pack(fill="x", pady=(int(6 * k), 0))

        # холст со страницами
        mid = tk.Frame(body, bg=BG)
        mid.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.status = tk.Label(self.win, bg=PANEL, fg=MUTED, font=font(9), anchor="w", padx=int(12 * k), pady=int(5 * k))
        self.status.pack(side="bottom", fill="x")

        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<Button-3>", self.on_context)
        for w in (c, self.win):
            w.bind("<MouseWheel>", self.on_wheel)
            w.bind("<Button-4>", lambda e: self._wheel_steps(e, -1))
            w.bind("<Button-5>", lambda e: self._wheel_steps(e, 1))

        if dnd_available(self.win):
            from tkinterdnd2 import DND_FILES
            c.drop_target_register(DND_FILES)
            c.dnd_bind("<<DropPosition>>", self._dnd_position)
            c.dnd_bind("<<DropLeave>>", lambda e: (c.delete("ins"), e.action)[1])
            c.dnd_bind("<<Drop>>", self._dnd_drop)

        self.menu = tk.Menu(self.win, tearoff=0, bg=CARD, fg=TEXT, activebackground=ACCENT,
                            activeforeground="#FFFFFF", bd=0, font=font(10))
        for item in [("Повернуть по часовой", lambda: self.rotate(90)),
                           ("Повернуть против часовой", lambda: self.rotate(-90)),
                           ("Дублировать", self.duplicate), ("Вставить пустую после", self.insert_blank),
                           None, ("Переместить в начало", lambda: self.move_selected_to(0)),
                           ("Переместить в конец", lambda: self.move_selected_to(len(self.pages))),
                           None, ("Извлечь в отдельный PDF…", self.extract_selected),
                           None, ("Удалить", self.delete)]:
            if item is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(label=item[0], command=item[1])

    def _bind_keys(self):
        def on_key(e):
            key = shortcut_key(e)
            ctrl = bool(e.state & 0x4)
            shift = bool(e.state & 0x1)
            if e.keysym in ("Delete", "BackSpace"):
                self.delete()
            elif e.keysym == "Escape":
                self.sel.clear(); self.redraw()
            elif e.keysym in ("Left", "Right", "Up", "Down"):
                self.move_focus(e.keysym, shift)
            elif ctrl and key == "z":
                self.redo() if shift else self.undo()
            elif ctrl and key == "y":
                self.redo()
            elif ctrl and key == "a":
                self.sel = {p.uid for p in self.pages}; self.redraw()
            elif ctrl and key == "s":
                self.save()
            elif ctrl and key == "d":
                self.duplicate()
            elif not ctrl and key == "r":
                self.rotate(-90 if shift else 90)
            else:
                return
            return "break"
        self.win.bind("<KeyPress>", on_key)

    # ───────────────── данные ─────────────────
    def snapshot(self):
        return ([dataclasses.replace(p) for p in self.pages], set(self.sel))

    def push_undo(self):
        self.undo_stack.append(self.snapshot())
        del self.undo_stack[:-80]
        self.redo_stack.clear()
        self.dirty = True

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.pages, self.sel = self.undo_stack.pop()
            self.redraw()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.pages, self.sel = self.redo_stack.pop()
            self.redraw()

    def index_of(self, uid):
        for i, p in enumerate(self.pages):
            if p.uid == uid:
                return i
        return -1

    def selected_pages(self) -> list[Page]:
        return [p for p in self.pages if p.uid in self.sel]

    def insert_point(self) -> int:
        """Куда вставлять новое: после последней выделенной страницы, иначе в конец."""
        idx = [i for i, p in enumerate(self.pages) if p.uid in self.sel]
        return idx[-1] + 1 if idx else len(self.pages)

    def add_files(self, paths: list[Path], at: int | None = None):
        new_pages = []
        errors = []
        for p in paths:
            if p.is_dir():
                continue
            try:
                doc = engine.open_as_pdf(p)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
                continue
            if getattr(doc, "needs_pass", False):
                pw = simpledialog.askstring("Пароль", f"PDF «{p.name}» защищён паролем:", show="•", parent=self.win)
                if not pw or not doc.authenticate(pw):
                    errors.append(f"{p.name}: неверный пароль")
                    continue
            src = Source(p, doc, FILE_COLORS[len(self.sources) % len(FILE_COLORS)])
            self.sources.append(src)
            di = len(self.sources) - 1
            for i in range(len(doc)):
                r = doc[i].rect
                new_pages.append(Page(next(_uid), di, i, 0, r.width, r.height))
        if new_pages:
            self.push_undo()
            at = len(self.pages) if at is None else at
            self.pages[at:at] = new_pages
            self.sel = {p.uid for p in new_pages} if self.pages != new_pages else set()
            self.dirty = len(self.sources) > 1 or self.dirty
        self.refresh_files()
        self.redraw()
        if errors:
            messagebox.showwarning("Не все файлы добавлены", "\n".join(errors), parent=self.win)

    def ask_add(self):
        init = str(self.sources[-1].path.parent) if self.sources else str(Path.home())
        files = filedialog.askopenfilenames(
            parent=self.win, title="Добавить PDF или картинки", initialdir=init,
            filetypes=[("PDF и изображения", "*.pdf *.jpg *.jpeg *.png *.webp *.heic *.heif *.bmp *.tif *.tiff *.gif"),
                       ("Все файлы", "*.*")])
        if files:
            self.add_files([Path(f) for f in files], self.insert_point())

    # ───────────────── операции ─────────────────
    def _targets(self) -> list[Page]:
        sel = self.selected_pages()
        if not sel and self.hover_uid is not None:
            sel = [p for p in self.pages if p.uid == self.hover_uid]
        return sel

    def rotate(self, deg, pages=None):
        pages = pages or self._targets()
        if not pages:
            return self.hint("Выделите страницы, которые нужно повернуть")
        self.push_undo()
        uids = {p.uid for p in pages}
        for p in self.pages:
            if p.uid in uids:
                p.rot = (p.rot + deg) % 360
        self.redraw()

    def delete(self, pages=None):
        pages = pages or self._targets()
        if not pages:
            return self.hint("Выделите страницы для удаления")
        self.push_undo()
        uids = {p.uid for p in pages}
        self.pages = [p for p in self.pages if p.uid not in uids]
        self.sel -= uids
        self.redraw()

    def duplicate(self):
        pages = self._targets()
        if not pages:
            return self.hint("Выделите страницы, которые нужно продублировать")
        self.push_undo()
        uids = {p.uid for p in pages}
        out, new_sel = [], set()
        for p in self.pages:
            out.append(p)
            if p.uid in uids:
                c = dataclasses.replace(p, uid=next(_uid))
                out.append(c)
                new_sel.add(c.uid)
        self.pages, self.sel = out, new_sel
        self.redraw()

    def insert_blank(self):
        at = self.insert_point()
        ref = self.pages[at - 1] if at > 0 and self.pages else None
        w, h = (ref.w, ref.h) if ref else (595.0, 842.0)
        if ref and ref.rot in (90, 270):
            w, h = h, w
        self.push_undo()
        p = Page(next(_uid), -1, None, 0, w, h)
        self.pages.insert(at, p)
        self.sel = {p.uid}
        self.redraw()

    def reverse(self):
        if len(self.pages) < 2:
            return
        self.push_undo()
        idx = [i for i, p in enumerate(self.pages) if p.uid in self.sel]
        if len(idx) >= 2:
            chosen = [self.pages[i] for i in idx][::-1]
            for i, p in zip(idx, chosen):
                self.pages[i] = p
        else:
            self.pages.reverse()
        self.redraw()

    def sort_by_file(self):
        self.push_undo()
        self.pages.sort(key=lambda p: (p.doc if p.doc >= 0 else 10 ** 6, p.pno if p.pno is not None else 0))
        self.redraw()

    def interleave(self):
        groups: dict[int, list[Page]] = {}
        for p in self.pages:
            groups.setdefault(p.doc, []).append(p)
        if len([g for g in groups if g >= 0]) < 2:
            return self.hint("Чередование работает, когда добавлено хотя бы два файла")
        self.push_undo()
        lists = [groups[g] for g in sorted(groups) if g >= 0] + ([groups[-1]] if -1 in groups else [])
        out = []
        for row in itertools.zip_longest(*lists):
            out.extend(p for p in row if p is not None)
        self.pages = out
        self.redraw()

    def move_pages(self, uids: set[int], to_index: int):
        moving = [p for p in self.pages if p.uid in uids]
        if not moving:
            return
        before = sum(1 for p in self.pages[:to_index] if p.uid in uids)
        rest = [p for p in self.pages if p.uid not in uids]
        pos = max(0, min(len(rest), to_index - before))
        new = rest[:pos] + moving + rest[pos:]
        if [p.uid for p in new] == [p.uid for p in self.pages]:
            return
        self.push_undo()
        self.pages = new
        self.redraw()

    def move_selected_to(self, idx):
        self.move_pages(set(self.sel), idx)

    def move_focus(self, direction, extend):
        if not self.pages:
            return
        cur = self.index_of(self.anchor) if self.anchor is not None else -1
        step = {"Left": -1, "Right": 1, "Up": -self.cols, "Down": self.cols}[direction]
        nxt = min(len(self.pages) - 1, max(0, cur + step if cur >= 0 else 0))
        uid = self.pages[nxt].uid
        if extend and self.sel:
            self.sel.add(uid)
        else:
            self.sel = {uid}
        self.anchor = uid
        self.redraw()
        self.scroll_into_view(uid)

    # ───────────────── построение и сохранение ─────────────────
    def build_doc(self, pages: list[Page]):
        fitz = engine._fitz()
        out = fitz.open()
        for p in pages:
            if p.pno is None:
                out.new_page(width=p.w, height=p.h)
                continue
            out.insert_pdf(self.sources[p.doc].doc, from_page=p.pno, to_page=p.pno)
            if p.rot:
                pg = out[len(out) - 1]
                pg.set_rotation((pg.rotation + p.rot) % 360)
        return out

    def _default_name(self, suffix):
        first = self.sources[0].path if self.sources else Path.home() / "документ.pdf"
        return first.parent, f"{first.stem}{suffix}.pdf"

    def _write(self, pages, path: Path) -> Path:
        self.status.configure(text="Сохраняю…")
        self.win.configure(cursor="watch")
        self.win.update_idletasks()
        try:
            doc = self.build_doc(pages)
            doc.save(path, garbage=3, deflate=True)
            doc.close()
        finally:
            self.win.configure(cursor="")
        return path

    def save(self):
        if not self.pages:
            return self.hint("Добавьте страницы")
        folder, name = self._default_name(" (упорядочен)")
        f = filedialog.asksaveasfilename(parent=self.win, title="Сохранить PDF", initialdir=str(folder),
                                         initialfile=name, defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not f:
            return
        dst = Path(f)
        overwrite = any(src.path.resolve() == dst.resolve() for src in self.sources if dst.exists())
        if overwrite:
            # перезапись исходника: пишем во временный файл, закрываем всё, подменяем
            tmp = dst.with_name(dst.stem + ".mandarin-tmp.pdf")
            self._write(self.pages, tmp)
            for src in self.sources:
                src.doc.close()
            os.replace(tmp, dst)
            # старые ссылки на страницы больше не валидны — открываем результат заново
            doc = engine.open_as_pdf(dst)
            self.sources = [Source(dst, doc, FILE_COLORS[0])]
            self.pages = [Page(next(_uid), 0, i, 0, doc[i].rect.width, doc[i].rect.height) for i in range(len(doc))]
            self.sel.clear()
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.thumbs.clear()
            self.refresh_files()
            self.redraw()
        else:
            self._write(self.pages, dst)
        self.dirty = False
        self.done("PDF сохранён ✓", f"{dst.name} · страниц: {len(self.pages)}", dst)

    def extract_selected(self):
        pages = self.selected_pages()
        if not pages:
            return self.hint("Выделите страницы, которые нужно извлечь")
        folder, name = self._default_name(" (выдержка)")
        f = filedialog.asksaveasfilename(parent=self.win, title="Извлечь страницы в PDF", initialdir=str(folder),
                                         initialfile=name, defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if f:
            self._write(pages, Path(f))
            self.done("Страницы извлечены ✓", f"{Path(f).name} · страниц: {len(pages)}", Path(f))

    def split_all(self):
        pages = self.selected_pages() or self.pages
        if not pages:
            return self.hint("Добавьте страницы")
        folder, _ = self._default_name("")
        d = filedialog.askdirectory(parent=self.win, title="Папка для страниц", initialdir=str(folder))
        if not d:
            return
        stem = self.sources[0].path.stem if self.sources else "страница"
        out_dir = engine.unique(Path(d) / f"{stem} — по страницам")
        out_dir.mkdir()
        width = len(str(len(pages)))
        for i, p in enumerate(pages, 1):
            self._write([p], out_dir / f"{stem} — {i:0{width}d}.pdf")
        self.done("Разделено ✓", f"{len(pages)} файлов в «{out_dir.name}»", out_dir)

    def export_png(self):
        pages = self.selected_pages() or self.pages
        if not pages:
            return self.hint("Добавьте страницы")
        folder, _ = self._default_name("")
        d = filedialog.askdirectory(parent=self.win, title="Папка для картинок", initialdir=str(folder))
        if not d:
            return
        doc = self.build_doc(pages)
        stem = self.sources[0].path.stem if self.sources else "страница"
        out_dir = engine.unique(Path(d) / f"{stem} — картинки")
        out_dir.mkdir()
        width = len(str(len(pages)))
        for i, page in enumerate(doc, 1):
            page.get_pixmap(dpi=200).save(out_dir / f"{stem} — {i:0{width}d}.png")
        doc.close()
        self.done("Готово ✓", f"{len(pages)} PNG в «{out_dir.name}»", out_dir)

    def done(self, title, body, path):
        self.status.configure(text=f"{title}  {body}")
        if self.on_done:
            self.on_done(title, body, path)

    def close(self):
        if self.dirty and self.pages:
            ans = messagebox.askyesnocancel("Сохранить изменения?", "Сохранить результат перед закрытием?",
                                            parent=self.win)
            if ans is None:
                return
            if ans:
                self.save()
                if self.dirty:
                    return
        for s in self.sources:
            try:
                s.doc.close()
            except Exception:
                pass
        self.win.destroy()

    def hint(self, text):
        self.status.configure(text=text, fg=ACCENT)
        self.win.after(2500, self.update_status)

    # ───────────────── отрисовка ─────────────────
    def refresh_files(self):
        for w in self.files_box.winfo_children():
            w.destroy()
        k = self.k
        if not self.sources:
            tk.Label(self.files_box, text="Пока пусто", bg=PANEL, fg=MUTED, font=font(9), anchor="w").pack(fill="x")
        for s in self.sources:
            row = tk.Frame(self.files_box, bg=PANEL)
            row.pack(fill="x", pady=int(2 * k))
            tk.Frame(row, bg=s.color, width=int(10 * k), height=int(10 * k)).pack(side="left", padx=(0, int(8 * k)))
            name = s.path.name if len(s.path.name) <= 30 else s.path.name[:28] + "…"
            tk.Label(row, text=name, bg=PANEL, fg=TEXT, font=font(9), anchor="w").pack(side="left")
            tk.Label(row, text=str(len(s.doc)), bg=PANEL, fg=MUTED, font=font(9)).pack(side="right")

    def metrics(self):
        k = self.k
        box = self.zoom
        pad = int(14 * k)
        gap = int(18 * k)
        card_w = box + pad
        card_h = box + pad + int(26 * k)
        width = max(self.canvas.winfo_width(), card_w + 2 * gap)
        cols = max(1, (width - gap) // (card_w + gap))
        left = (width - cols * card_w - (cols - 1) * gap) // 2
        return box, pad, gap, card_w, card_h, cols, left

    def redraw(self):
        c = self.canvas
        c.delete("all")
        self.img_items.clear()
        self.geom.clear()
        box, pad, gap, cw, ch, cols, left = self.metrics()
        self.cols = cols
        k = self.k
        if not self.pages:
            w, h = c.winfo_width(), c.winfo_height()
            c.create_text(w / 2, h / 2 - 20 * k, text="Перетащите сюда PDF или картинки",
                          fill=TEXT, font=font(16, "bold"))
            c.create_text(w / 2, h / 2 + 14 * k, text="или нажмите «＋ Добавить файлы» вверху",
                          fill=MUTED, font=font(11))
            c.configure(scrollregion=(0, 0, w, h))
            self.update_status()
            return
        sel_color = ACCENT
        for i, p in enumerate(self.pages):
            r, col = divmod(i, cols)
            x0 = left + col * (cw + gap)
            y0 = gap + r * (ch + gap)
            x1, y1 = x0 + cw, y0 + ch
            self.geom[p.uid] = (x0, y0, x1, y1)
            selected = p.uid in self.sel
            hovered = p.uid == self.hover_uid
            fill = "#FAFAFB" if hovered else "#FFFFFF"
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=sel_color if selected else LINE,
                               width=max(2, int(3 * k)) if selected else 1, tags=("card", f"u{p.uid}"))
            # миниатюра
            cx, cy = x0 + cw / 2, y0 + pad / 2 + box / 2
            img = self.thumbs.get(self._thumb_key(p))
            if img is not None:
                self.img_items[p.uid] = c.create_image(cx, cy, image=img)
            else:
                w, h = self._fit(p)
                c.create_rectangle(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, fill="#FFFFFF" if p.pno is None else "#DADADF",
                                   outline="")
                if p.pno is not None:
                    self.img_items[p.uid] = c.create_image(cx, cy)
                    self.pending[p.uid] = p
                else:
                    c.create_text(cx, cy, text="пустая", fill="#A0A0AA", font=font(9))
            # подпись: номер и цвет файла
            ty = y1 - int(13 * k)
            color = self.sources[p.doc].color if p.doc >= 0 else MUTED
            c.create_rectangle(x0 + pad / 2, ty - 4 * k, x0 + pad / 2 + 8 * k, ty + 4 * k, fill=color, outline="")
            c.create_text(cx, ty, text=str(i + 1), fill=TEXT if selected else MUTED, font=font(10, "bold"))
            if p.rot:
                c.create_text(x1 - pad / 2, ty, text=f"↻{p.rot}°", fill=MUTED, font=font(8), anchor="e")
            if hovered and not self.drag:
                self._draw_hover_buttons(p.uid)
        rows = math.ceil(len(self.pages) / cols)
        total_h = gap + rows * (ch + gap)
        c.configure(scrollregion=(0, 0, c.winfo_width(), max(total_h, c.winfo_height())))
        self.update_status()
        if self.pending and not getattr(self, "_render_job", None):
            self._render_job = self.win.after(1, self._render_some)

    def _hover_buttons_geom(self, uid):
        x0, y0, x1, y1 = self.geom[uid]
        r = 13 * self.k
        return {"rot": (x1 - 3.4 * r, y0 + 1.6 * r, r), "del": (x1 - 1.4 * r, y0 + 1.6 * r, r)}

    def _draw_hover_buttons(self, uid):
        c = self.canvas
        for name, (bx, by, r) in self._hover_buttons_geom(uid).items():
            c.create_oval(bx - r, by - r, bx + r, by + r, fill=DANGER if name == "del" else "#2A2A31",
                          outline="", tags=("hb",))
            c.create_text(bx, by, text="✕" if name == "del" else "↻", fill="#FFFFFF",
                          font=font(10, "bold"), tags=("hb",))

    def _thumb_key(self, p: Page):
        return (p.doc, p.pno, p.rot, self.zoom)

    def _fit(self, p: Page):
        w, h = (p.h, p.w) if p.rot in (90, 270) else (p.w, p.h)
        s = self.zoom / max(w, h)
        return w * s, h * s

    def _render_some(self):
        self._render_job = None
        from PIL import Image, ImageTk
        t_end = 6
        done = 0
        while self.pending and done < t_end:
            p = self.pending.pop(next(iter(self.pending)))
            key = self._thumb_key(p)
            if key in self.thumbs or p.uid not in self.img_items:
                continue
            try:
                page = self.sources[p.doc].doc[p.pno]
                rect = page.rect
                scale = self.zoom / max(rect.width, rect.height)
                pix = page.get_pixmap(matrix=engine._fitz().Matrix(scale, scale), alpha=False)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if p.rot:
                    im = im.rotate(-p.rot, expand=True)
                self.thumbs[key] = ImageTk.PhotoImage(im)
                if p.uid in self.img_items:
                    self.canvas.itemconfigure(self.img_items[p.uid], image=self.thumbs[key])
                    self.canvas.tag_raise(self.img_items[p.uid])
                    self.canvas.tag_raise("hb")
            except Exception:
                pass
            done += 1
        if len(self.thumbs) > 1500:
            for key in list(self.thumbs)[:500]:
                self.thumbs.pop(key, None)
        if self.pending:
            self._render_job = self.win.after(1, self._render_some)

    def update_status(self):
        n, s = len(self.pages), len(self.sel)
        text = f"Страниц: {n}" + (f"  ·  выделено: {s}" if s else "") + \
            "     Перетаскивайте страницы · R — повернуть · Delete — удалить · Ctrl+Z — отменить · двойной клик — просмотр"
        self.status.configure(text=text, fg=MUTED)

    def scroll_into_view(self, uid):
        if uid not in self.geom:
            return
        _, y0, _, y1 = self.geom[uid]
        c = self.canvas
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        total = float(c.cget("scrollregion").split()[3]) or 1
        if y0 < top:
            c.yview_moveto(max(0, y0 - 10) / total)
        elif y1 > bottom:
            c.yview_moveto((y1 - c.winfo_height() + 10) / total)

    # ───────────────── мышь ─────────────────
    def uid_at(self, x, y):
        for uid, (x0, y0, x1, y1) in self.geom.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return uid
        return None

    def gap_index_at(self, x, y) -> int:
        """Индекс вставки (0…len) по позиции курсора — ближайший промежуток между карточками."""
        if not self.pages:
            return 0
        box, pad, gap, cw, ch, cols, left = self.metrics()
        row = max(0, int((y - gap / 2) // (ch + gap)))
        col_f = (x - left + gap / 2) / (cw + gap)
        col = max(0, min(cols, int(round(col_f))))
        idx = row * cols + col
        return max(0, min(len(self.pages), idx))

    def draw_insert_marker(self, idx):
        c = self.canvas
        c.delete("ins")
        box, pad, gap, cw, ch, cols, left = self.metrics()
        r, col = divmod(idx, cols)
        if col == 0 and idx > 0 and idx == len(self.pages) and len(self.pages) % cols == 0:
            r, col = r - 1, cols
        x = left + col * (cw + gap) - gap / 2
        y0 = gap + r * (ch + gap)
        c.create_line(x, y0 - 4, x, y0 + ch + 4, fill=ACCENT, width=max(3, int(4 * self.k)), tags=("ins",))

    def _set_hover(self, uid):
        if uid != self.hover_uid:
            self.hover_uid = uid
            if not self.drag:
                self.redraw()

    def on_hover(self, e):
        if self.drag:
            return
        self._set_hover(self.uid_at(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)))

    def _hit_hover_button(self, uid, x, y):
        if uid is None or uid not in self.geom:
            return None
        for name, (bx, by, r) in self._hover_buttons_geom(uid).items():
            if math.hypot(x - bx, y - by) <= r + 2:
                return name
        return None

    def on_press(self, e):
        self.win.focus_set()
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        uid = self.uid_at(x, y)
        btn = self._hit_hover_button(uid, x, y)
        if btn:
            page = [p for p in self.pages if p.uid == uid]
            self.rotate(90, page) if btn == "rot" else self.delete(page)
            self.drag = None
            return
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        self.drag = {"uid": uid, "x": e.x, "y": e.y, "started": False, "deferred": False}
        if uid is None:
            if not ctrl and not shift:
                self.sel.clear()
            self.redraw()
            return
        if ctrl:
            self.sel ^= {uid}
        elif shift and self.anchor is not None and self.index_of(self.anchor) >= 0:
            a, b = sorted((self.index_of(self.anchor), self.index_of(uid)))
            self.sel = {p.uid for p in self.pages[a:b + 1]}
        elif uid in self.sel:
            self.drag["deferred"] = True   # возможно, тащим всю группу
        else:
            self.sel = {uid}
        if not shift:
            self.anchor = uid
        self.redraw()

    def on_motion(self, e):
        d = self.drag
        if not d or d["uid"] is None:
            return
        if not d["started"]:
            if math.hypot(e.x - d["x"], e.y - d["y"]) < 6:
                return
            d["started"] = True
            d["uids"] = set(self.sel) if d["uid"] in self.sel else {d["uid"]}
            self.redraw()
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        d["idx"] = self.gap_index_at(x, y)
        self.draw_insert_marker(d["idx"])
        c = self.canvas
        c.delete("ghost")
        n = len(d["uids"])
        s = 34 * self.k
        c.create_rectangle(x + 10, y + 10, x + 10 + s * 1.4, y + 10 + s, fill=ACCENT, outline="", tags=("ghost",))
        c.create_text(x + 10 + s * 0.7, y + 10 + s / 2, text=f"{n} стр." if n > 1 else "1 стр.",
                      fill="#FFFFFF", font=font(9, "bold"), tags=("ghost",))
        d["ey"] = e.y
        if not self.scroll_job:
            self.scroll_job = self.win.after(30, self._autoscroll)

    def _autoscroll(self):
        self.scroll_job = None
        d = self.drag
        if not d or not d.get("started"):
            return
        h = self.canvas.winfo_height()
        edge = 50 * self.k
        y = d.get("ey", h / 2)
        if y < edge:
            self.canvas.yview_scroll(-1, "units")
        elif y > h - edge:
            self.canvas.yview_scroll(1, "units")
        else:
            return
        self.scroll_job = self.win.after(40, self._autoscroll)

    def on_release(self, e):
        d, self.drag = self.drag, None
        self.canvas.delete("ins", "ghost")
        if not d:
            return
        if d["started"]:
            self.move_pages(d["uids"], d.get("idx", 0))
            self.redraw()
        elif d["deferred"]:
            self.sel = {d["uid"]}
            self.redraw()

    def on_double(self, e):
        uid = self.uid_at(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if uid is not None:
            self.preview(uid)

    def on_context(self, e):
        uid = self.uid_at(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if uid is None:
            return
        if uid not in self.sel:
            self.sel = {uid}
            self.anchor = uid
            self.redraw()
        self.menu.tk_popup(e.x_root, e.y_root)

    def on_wheel(self, e):
        if e.state & 0x4:   # Ctrl+колесо — масштаб
            self.set_zoom(self.zoom + (12 if e.delta > 0 else -12) * self.k)
            return "break"
        self.canvas.yview_scroll(int(-e.delta / 120) * 2 if IS_WIN else -int(e.delta), "units")
        return "break"

    def _wheel_steps(self, e, step):
        if e.state & 0x4:
            self.set_zoom(self.zoom - step * 12 * self.k)
        else:
            self.canvas.yview_scroll(step * 2, "units")
        return "break"

    def set_zoom(self, z):
        z = int(max(self.MIN_ZOOM * self.k, min(self.MAX_ZOOM * self.k, z)))
        if z != self.zoom:
            self.zoom = z
            self.pending.clear()
            self.redraw()

    # ── перетаскивание файлов в окно ──
    def _dnd_position(self, e):
        x = e.x_root - self.canvas.winfo_rootx()
        y = e.y_root - self.canvas.winfo_rooty()
        self.draw_insert_marker(self.gap_index_at(self.canvas.canvasx(x), self.canvas.canvasy(y)))
        return "copy"

    def _dnd_drop(self, e):
        self.canvas.delete("ins")
        x = e.x_root - self.canvas.winfo_rootx()
        y = e.y_root - self.canvas.winfo_rooty()
        idx = self.gap_index_at(self.canvas.canvasx(x), self.canvas.canvasy(y))
        files = [Path(f) for f in self.win.tk.splitlist(e.data)]
        self.win.after(10, lambda: self.add_files(files, idx))
        return "copy"

    # ───────────────── просмотр страницы ─────────────────
    def preview(self, uid):
        p = next((p for p in self.pages if p.uid == uid), None)
        if not p or p.pno is None:
            return
        from PIL import Image, ImageTk
        top = tk.Toplevel(self.win)
        top.title(f"Страница {self.index_of(uid) + 1}")
        top.configure(bg=BG)
        sh = int(top.winfo_screenheight() * 0.82)
        page = self.sources[p.doc].doc[p.pno]
        scale = sh / max(page.rect.width, page.rect.height) if p.rot in (90, 270) else sh / page.rect.height
        pix = page.get_pixmap(matrix=engine._fitz().Matrix(scale, scale), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if p.rot:
            im = im.rotate(-p.rot, expand=True)
        photo = ImageTk.PhotoImage(im)
        lbl = tk.Label(top, image=photo, bg=BG)
        lbl.image = photo
        lbl.pack(padx=10, pady=10)
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Button-1>", lambda e: top.destroy())
        top.focus_set()


def reveal(path: Path):
    if IS_WIN:
        if path.is_dir():
            subprocess.Popen(["explorer", str(path)])
        else:
            subprocess.Popen(["explorer", "/select,", str(path)])


def main(argv):
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    root.withdraw()
    org = PdfOrganizer(root, [a for a in argv if Path(a).exists()],
                       on_done=lambda t, b, p: reveal(p))
    org.win.bind("<Destroy>", lambda e: e.widget is org.win and root.destroy())
    root.mainloop()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
