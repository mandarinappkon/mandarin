"""
Mandarin PDF — просмотр и редактирование PDF (функции уровня Acrobat Pro, офлайн).

Слева — закладки, в центре — страницы, справа — панель инструментов:
  Комментарии · Заполнить и подписать · Правка · Защита и зачернение · Оформление · Формы · Документ.

Документ открывается в память (файл на диске не блокируется). Каждое изменение можно отменить
(Ctrl+Z). «Сохранить» по умолчанию предлагает новое имя, оригинал не трогается.

Отдельный запуск:  python viewer.py файл.pdf
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import dialogs
import engine
import pdftools
from ui_kit import (ACCENT, BG, CARD, DANGER, IS_WIN, LINE, MUTED, PANEL, TEXT, FlatButton,
                    dnd_available, font, shortcut_key, tooltip, ui_scale)

fitz = engine._fitz()

GAP = 18            # промежуток между страницами (px)
SEL_FILL = "#4C8DFF"

# инструменты: id → (подпись, подсказка, курсор)
TOOLS = {
    "select": ("Выделение", "Выделить и скопировать текст, выбрать/двигать комментарий", "xterm"),
    "hand": ("Рука", "Прокрутка перетаскиванием", "fleur"),
    "highlight": ("Маркер", "Выделите текст — он подсветится жёлтым", "xterm"),
    "underline": ("Подчеркн.", "Выделите текст для подчёркивания", "xterm"),
    "strike": ("Зачеркн.", "Выделите текст для зачёркивания", "xterm"),
    "note": ("Заметка", "Щёлкните, чтобы оставить заметку-стикер", "crosshair"),
    "text": ("Текст", "Обведите рамку и введите текст", "crosshair"),
    "ink": ("Рисовать", "Рисуйте мышью от руки", "pencil"),
    "rect": ("Прямоуг.", "Обведите прямоугольник", "crosshair"),
    "ellipse": ("Овал", "Обведите овал", "crosshair"),
    "line": ("Линия", "Проведите линию", "crosshair"),
    "arrow": ("Стрелка", "Проведите стрелку", "crosshair"),
    "erase": ("Ластик", "Щёлкните по комментарию, чтобы удалить", "X_cursor"),
    "sign": ("Подпись", "Щёлкните, где поставить подпись (ПКМ по кнопке — новая подпись)", "crosshair"),
    "date": ("Дата", "Щёлкните, чтобы вписать сегодняшнюю дату", "crosshair"),
    "check": ("Галочка", "Щёлкните, чтобы поставить ✓", "crosshair"),
    "stamp": ("Штамп", "Щёлкните, где поставить штамп", "crosshair"),
    "image": ("Картинка", "Щёлкните, куда вставить изображение", "crosshair"),
    "fill": ("Заполнить", "Щёлкайте по полям формы и вводите значения", "hand2"),
    "edit_text": ("Изменить текст", "Щёлкните по строке текста, чтобы исправить её", "xterm"),
    "redact": ("Зачернить", "Обведите область — после «Применить» она удалится навсегда", "crosshair"),
    "f_text": ("Поле текста", "Обведите место для текстового поля", "crosshair"),
    "f_check": ("Флажок", "Обведите место для флажка", "crosshair"),
    "f_combo": ("Список", "Обведите место для выпадающего списка", "crosshair"),
    "f_sign": ("Поле подписи", "Обведите место для подписи", "crosshair"),
}

ANNOT_COLORS = {"highlight": (1, 0.85, 0.1), "underline": (0.1, 0.45, 0.95), "strike": (0.9, 0.15, 0.2),
                "ink": (0.1, 0.16, 0.42), "shape": (0.9, 0.2, 0.15)}


class PdfViewer:
    def __init__(self, master, path: Path | None = None, on_done=None, on_open_organizer=None,
                 password: str | None = None):
        self.k = k = ui_scale(master)
        self.master = master
        self.on_done = on_done                  # уведомления (title, body, path)
        self.on_open_organizer = on_open_organizer
        self.win = tk.Toplevel(master)
        self.win.configure(bg=BG)
        self.win.geometry(f"{int(1320 * k)}x{int(860 * k)}")
        self.win.minsize(int(900 * k), int(560 * k))
        self.doc = None
        self.path: Path | None = None
        self.save_path: Path | None = None
        self.password = password
        self.zoom = 1.3 * k
        self.fit_width = True
        self.layout: list[tuple[float, float, float, float]] = []
        self.images: dict[int, object] = {}
        self.img_items: dict[int, int] = {}
        self.page_ver: dict[int, int] = {}
        self.words_cache: dict[int, list] = {}
        self.tool = "select"
        self.undo_stack: list[bytes] = []
        self.redo_stack: list[bytes] = []
        self.dirty = False
        self.sel_words: list[tuple[int, int]] = []     # (страница, индекс слова)
        self.sel_annot = None                           # (страница, xref)
        self.drag = None
        self.search_hits: list[tuple[int, object]] = []
        self.search_i = -1
        self.jobs: queue.Queue = queue.Queue()
        self.render_job = None
        self.tool_buttons: dict[str, tk.Label] = {}
        self._pages: dict[int, object] = {}
        self.stamp_choice = None
        self.signature_png = None

        self._build()
        self._bind()
        if path:
            self.win.after(20, lambda: self.open(Path(path)))
        else:
            self._update_title()
            self.redraw()
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.after(100, self._poll_jobs)

    # ═════════════════════ интерфейс ═════════════════════
    def _build(self):
        k = self.k
        top = tk.Frame(self.win, bg=PANEL, padx=int(10 * k), pady=int(7 * k))
        top.pack(side="top", fill="x")

        def tb(text, cmd, tip=None, kind="normal"):
            b = FlatButton(top, text, cmd, kind=kind, size=10, padx=int(9 * k), pady=int(5 * k))
            b.pack(side="left", padx=(0, int(5 * k)))
            if tip:
                tooltip(b, tip)
            return b

        tb("Открыть", self.ask_open, "Открыть PDF (Ctrl+O)")
        tb("Сохранить", self.save, "Сохранить (Ctrl+S)", "accent")
        tb("Как…", self.save_as, "Сохранить как")
        tb("Печать", self.print_doc, "Печать (Ctrl+P)")
        self._sep(top)
        tb("↶", self.undo, "Отменить (Ctrl+Z)")
        tb("↷", self.redo, "Вернуть (Ctrl+Y)")
        self._sep(top)
        tb("−", lambda: self.set_zoom(self.zoom / 1.2), "Уменьшить")
        self.zoom_lbl = tk.Label(top, text="100%", bg=PANEL, fg=MUTED, font=font(9), width=5)
        self.zoom_lbl.pack(side="left")
        tb("+", lambda: self.set_zoom(self.zoom * 1.2), "Увеличить")
        tb("↔", self.fit, "По ширине")
        self._sep(top)
        self.page_var = tk.StringVar(value="0")
        e = tk.Entry(top, textvariable=self.page_var, width=4, font=font(10), justify="center", bd=0,
                     bg=CARD, fg=TEXT, relief="flat")
        e.pack(side="left", ipady=int(4 * k))
        e.bind("<Return>", lambda ev: self.goto(int(self.page_var.get() or 1) - 1))
        self.pages_lbl = tk.Label(top, text="/ 0", bg=PANEL, fg=MUTED, font=font(10))
        self.pages_lbl.pack(side="left", padx=(int(4 * k), int(8 * k)))

        # поиск справа
        sf = tk.Frame(top, bg=PANEL)
        sf.pack(side="right")
        self.search_var = tk.StringVar()
        line = tk.Frame(sf, bg=LINE, padx=1, pady=1)
        line.pack(side="left")
        self.search_entry = tk.Entry(line, textvariable=self.search_var, width=22, font=font(10), bd=0,
                                     bg="#FFFFFF", fg=TEXT, relief="flat")
        self.search_entry.pack(ipady=int(4 * k), padx=int(6 * k))
        self.search_entry.bind("<Return>", lambda ev: self.search(+1))
        self.search_entry.bind("<Shift-Return>", lambda ev: self.search(-1))
        FlatButton(sf, "Найти", lambda: self.search(+1), size=9, padx=int(8 * k), pady=int(4 * k)).pack(side="left", padx=int(4 * k))
        self.search_lbl = tk.Label(sf, text="", bg=PANEL, fg=MUTED, font=font(9))
        self.search_lbl.pack(side="left")

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True)

        # правая панель инструментов (прокручиваемая)
        right = tk.Frame(body, bg=PANEL, width=int(312 * k))
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        rc = tk.Canvas(right, bg=PANEL, highlightthickness=0, width=int(296 * k))
        rsb = tk.Scrollbar(right, orient="vertical", command=rc.yview)
        rc.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")
        rc.pack(side="left", fill="both", expand=True)
        self.tools_frame = tk.Frame(rc, bg=PANEL, padx=int(12 * k), pady=int(10 * k))
        rc.create_window(0, 0, window=self.tools_frame, anchor="nw", width=int(294 * k))
        self.tools_frame.bind("<Configure>", lambda e: rc.configure(scrollregion=rc.bbox("all")))
        self.tools_canvas = rc
        self._build_tools()

        # левая панель: закладки
        left = tk.Frame(body, bg=PANEL, width=int(200 * k), padx=int(10 * k), pady=int(10 * k))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="ЗАКЛАДКИ", bg=PANEL, fg=MUTED, font=font(8, "bold"), anchor="w").pack(fill="x")
        self.toc_list = tk.Listbox(left, bg=PANEL, fg=TEXT, font=font(9), bd=0, highlightthickness=0,
                                   selectbackground=ACCENT, selectforeground="#FFFFFF", activestyle="none")
        self.toc_list.pack(fill="both", expand=True, pady=(int(4 * k), int(6 * k)))
        self.toc_list.bind("<<ListboxSelect>>", self._toc_jump)
        bf = tk.Frame(left, bg=PANEL)
        bf.pack(fill="x")
        FlatButton(bf, "+ Закладка", self.add_bookmark, size=9, padx=int(6 * k), pady=int(3 * k)).pack(side="left")
        FlatButton(bf, "Удалить", self.del_bookmark, size=9, padx=int(6 * k), pady=int(3 * k)).pack(side="right")

        mid = tk.Frame(body, bg=BG)
        mid.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg="#E9E9EC", highlightthickness=0, bd=0)
        vs = tk.Scrollbar(mid, orient="vertical", command=self._yview)
        hs = tk.Scrollbar(mid, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.status = tk.Label(self.win, bg=PANEL, fg=MUTED, font=font(9), anchor="w", padx=int(12 * k),
                               pady=int(5 * k))
        self.status.pack(side="bottom", fill="x")

        if dnd_available(self.win):
            from tkinterdnd2 import DND_FILES
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._dnd_drop)

    def _sep(self, parent):
        tk.Frame(parent, bg=LINE, width=1, height=int(24 * self.k)).pack(side="left", padx=int(7 * self.k))

    def _build_tools(self):
        k = self.k
        f = self.tools_frame

        def section(title):
            tk.Label(f, text=title.upper(), bg=PANEL, fg=MUTED, font=font(8, "bold"), anchor="w"
                     ).pack(fill="x", pady=(int(10 * k), int(4 * k)))
            g = tk.Frame(f, bg=PANEL)
            g.pack(fill="x")
            for col in range(3):
                g.grid_columnconfigure(col, weight=1, uniform="t")
            g._i = 0
            return g

        def tool(g, tid, wide=False):
            label, tip, _ = TOOLS[tid]
            b = tk.Label(g, text=label, bg=CARD, fg=TEXT, font=font(9), padx=int(4 * k), pady=int(6 * k),
                         cursor="hand2")
            if wide:
                if g._i % 3:
                    g._i += 3 - g._i % 3
                b.grid(row=g._i // 3, column=0, columnspan=3, sticky="ew", pady=(0, int(4 * k)))
                g._i += 3
            else:
                b.grid(row=g._i // 3, column=g._i % 3, sticky="nsew", padx=(0, int(4 * k)), pady=(0, int(4 * k)))
                g._i += 1
            b.bind("<Button-1>", lambda e: self.set_tool(tid))
            tooltip(b, tip)
            self.tool_buttons[tid] = b
            return b

        def action(g, label, cmd, tip=None, wide=False, accent=False):
            b = FlatButton(g, label, cmd, kind="accent" if accent else "normal", size=9,
                           padx=int(6 * k), pady=int(6 * k))
            if wide:
                if g._i % 3:
                    g._i += 3 - g._i % 3
                b.grid(row=g._i // 3, column=0, columnspan=3, sticky="ew", padx=(0, int(4 * k)),
                       pady=(0, int(4 * k)))
                g._i += 3
            else:
                b.grid(row=g._i // 3, column=g._i % 3, sticky="nsew", padx=(0, int(4 * k)), pady=(0, int(4 * k)))
                g._i += 1
            if tip:
                tooltip(b, tip)
            return b

        g = section("Просмотр")
        for t in ("select", "hand"):
            tool(g, t)
        g = section("Комментарии")
        for t in ("highlight", "underline", "strike", "note", "text", "ink", "rect", "ellipse", "line", "arrow",
                  "erase"):
            tool(g, t)
        g = section("Заполнить и подписать")
        for t in ("sign", "date", "check", "stamp", "fill", "image"):
            tool(g, t)
        sb = self.tool_buttons["sign"]
        sb.bind("<Button-3>", lambda e: self._new_signature())
        g = section("Правка")
        tool(g, "edit_text", wide=True)
        action(g, "Поворот ↻", self.rotate_page, "Повернуть текущую страницу")
        action(g, "Удалить стр.", self.delete_page, "Удалить текущую страницу")
        action(g, "Упорядочить страницы…", self.open_organizer, "Перетаскивание, поворот, объединение", wide=True)
        g = section("Защита и зачернение")
        tool(g, "redact")
        action(g, "Найти…", self.find_redact, "Найти e-mail, телефоны, паспорта и т. п.")
        action(g, "Применить", self.apply_redact, "Удалить отмеченное навсегда")
        action(g, "Защитить паролем…", lambda: self.file_op("protect"), wide=True)
        action(g, "Снять пароль…", lambda: self.file_op("unlock"), wide=True)
        g = section("Оформление")
        action(g, "Водяной знак…", self.do_watermark, wide=True)
        action(g, "Номера страниц…", self.do_numbers, wide=True)
        action(g, "Колонтитулы…", self.do_header_footer, wide=True)
        action(g, "Обрезать поля…", self.do_crop, wide=True)
        g = section("Формы")
        for t in ("f_text", "f_check", "f_combo", "f_sign"):
            tool(g, t)
        action(g, "Сгладить", self.do_flatten, "Вшить формы и комментарии в страницы")
        g = section("Документ")
        action(g, "Распознать текст (OCR)", self.do_ocr, "Скан → PDF с поиском и копированием", wide=True)
        action(g, "Сравнить с другим PDF…", lambda: self.file_op("compare"), wide=True)
        action(g, "Подписать сертификатом…", lambda: self.file_op("sign_cert"), wide=True)
        action(g, "Проверить подписи", lambda: self.file_op("verify"), wide=True)
        action(g, "Сохранить как PDF/A…", lambda: self.file_op("pdfa"), wide=True)
        action(g, "Экспорт: Word", lambda: self.file_op("docx"))
        action(g, "Excel", lambda: self.file_op("xlsx"))
        action(g, "PowerPoint", lambda: self.file_op("pptx"))
        self._highlight_tool()

    # ═════════════════════ открытие / сохранение ═════════════════════
    def ask_open(self):
        p = filedialog.askopenfilename(parent=self.win, title="Открыть PDF",
                                       filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")])
        if p:
            if self.doc and not self._confirm_discard():
                return
            self.open(Path(p))

    def open(self, path: Path, data: bytes | None = None, keep_path=False):
        try:
            if data is None:
                if engine.category(path) == "image":
                    data = engine.image_pdf_bytes(path)
                else:
                    data = path.read_bytes()
            doc = fitz.open("pdf", data)
            if doc.needs_pass:
                pw = self.password or simpledialog.askstring("Пароль", f"«{path.name}» защищён паролем:",
                                                              show="•", parent=self.win)
                if not pw or not doc.authenticate(pw):
                    messagebox.showerror("Mandarin", "Неверный пароль", parent=self.win)
                    return
                # дальше работаем с расшифрованной копией (needs_pass после authenticate не трогаем)
                data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
                doc = fitz.open("pdf", data)
                self.password = pw
        except Exception as e:
            messagebox.showerror("Mandarin", f"Не удалось открыть файл:\n{e}", parent=self.win)
            return
        if self.doc is not None:
            self._pages = {}
            self.doc.close()
        self.doc = doc
        if not keep_path:
            self.path = path
            self.save_path = None
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.dirty = False
        self._reset_caches()
        self.fit_width = True
        self._update_title()
        self._load_toc()
        self.win.after(10, self.fit)
        if any(page_needs for page_needs in (pdftools.page_needs_ocr(p) for p in self.doc)) and pdftools.ocr_available():
            self.hint("Похоже на скан без текста — распознайте его: Документ → «Распознать текст (OCR)»")
        sigs = self._has_signatures()
        if sigs:
            self.hint("В документе есть цифровые подписи — любые изменения сделают их недействительными")

    def _has_signatures(self) -> bool:
        try:
            return any(w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.field_value
                       for i in range(len(self.doc)) for w in self.pg(i).widgets())
        except Exception:
            return False

    def pg(self, i: int):
        """Страница из кэша. ВАЖНО: комментарии и поля PyMuPDF ссылаются на объект страницы — если он
        уничтожен сборщиком мусора, обращение к ним роняет процесс. Поэтому страницы живут, пока жив документ."""
        if i < 0:
            i += len(self.doc)
        page = self._pages.get(i)
        if page is None:
            page = self._pages[i] = self.doc[i]
        return page

    def _reset_caches(self):
        self._pages = {}
        self.images.clear()
        self.page_ver.clear()
        self.words_cache.clear()
        self.sel_words = []
        self.sel_annot = None
        self.search_hits = []

    def _update_title(self):
        name = self.path.name if self.path else "PDF"
        self.win.title(f"{'● ' if self.dirty else ''}{name} — Mandarin PDF")
        self.pages_lbl.configure(text=f"/ {len(self.doc) if self.doc else 0}")

    def default_save_path(self) -> Path:
        base = self.path or Path.home() / "документ.pdf"
        return engine.unique(base.with_name(base.stem + " (изменён).pdf"))

    def save(self):
        if not self.doc:
            return
        if not self.save_path:
            return self.save_as()
        self._write(self.save_path)

    def save_as(self):
        if not self.doc:
            return
        d = self.default_save_path()
        p = filedialog.asksaveasfilename(parent=self.win, title="Сохранить PDF", initialdir=str(d.parent),
                                         initialfile=d.name, defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if p:
            self.save_path = Path(p)
            self._write(self.save_path)

    def _write(self, dst: Path):
        try:
            data = self.doc.tobytes(garbage=3, deflate=True)
            tmp = dst.with_name(dst.name + ".mandarin-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        except Exception as e:
            messagebox.showerror("Mandarin", f"Не удалось сохранить:\n{e}", parent=self.win)
            return
        self.dirty = False
        self._update_title()
        self.done("Сохранено ✓", dst.name, dst)

    def print_doc(self):
        if not self.doc:
            return
        tmp = Path(tempfile.mkdtemp(prefix="mandarin-print-")) / (self.path.name if self.path else "печать.pdf")
        tmp.write_bytes(self.doc.tobytes())
        if IS_WIN:
            try:
                os.startfile(str(tmp), "print")
                self.hint("Отправлено на печать (через программу просмотра PDF по умолчанию)")
            except OSError:
                os.startfile(str(tmp))
        else:
            subprocess.Popen(["xdg-open", str(tmp)])

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        ans = messagebox.askyesnocancel("Сохранить изменения?", "Сохранить изменения перед закрытием?",
                                        parent=self.win)
        if ans is None:
            return False
        if ans:
            self.save()
            return not self.dirty
        return True

    def close(self):
        if not self._confirm_discard():
            return
        if self.doc:
            self._pages = {}
            self.doc.close()
        self.win.destroy()

    def done(self, title, body, path=None):
        self.status.configure(text=f"{title}  {body}", fg=TEXT)
        if self.on_done:
            self.on_done(title, body, path)

    def hint(self, text, error=False):
        self.status.configure(text=text, fg=DANGER if error else ACCENT)

    # ═════════════════════ изменения и отмена ═════════════════════
    def checkpoint(self):
        """Снимок перед изменением — для Ctrl+Z."""
        self.undo_stack.append(self.doc.tobytes())
        del self.undo_stack[:-25]
        self.redo_stack.clear()

    def changed(self, pages=None):
        self.dirty = True
        self._update_title()
        if pages is None:
            self.page_ver = {i: self.page_ver.get(i, 0) + 1 for i in range(len(self.doc))}
            self.words_cache.clear()
            self.images.clear()
            self._relayout()
        else:
            for p in pages:
                self.page_ver[p] = self.page_ver.get(p, 0) + 1
                self.words_cache.pop(p, None)
                self.images.pop(p, None)
        self.redraw()

    def _restore(self, data: bytes):
        page = self.current_page()
        self._pages = {}
        self.doc.close()
        self.doc = fitz.open("pdf", data)
        self._reset_caches()
        self._relayout()
        self.dirty = True
        self._update_title()
        self._load_toc()
        self.redraw()
        self.goto(min(page, len(self.doc) - 1), smooth=False)

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.doc.tobytes())
            self._restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.doc.tobytes())
            self._restore(self.redo_stack.pop())

    # ═════════════════════ раскладка и отрисовка ═════════════════════
    def _relayout(self):
        self.layout = []
        if not self.doc:
            return
        cw = max(self.canvas.winfo_width(), 200)
        y = GAP
        maxw = 0
        for page in self.doc:
            w, h = page.rect.width * self.zoom, page.rect.height * self.zoom
            maxw = max(maxw, w)
            self.layout.append([0, y, w, h])
            y += h + GAP
        total_w = max(cw, maxw + 2 * GAP)
        for item in self.layout:
            item[0] = (total_w - item[2]) / 2
        self.canvas.configure(scrollregion=(0, 0, total_w, y))
        self.zoom_lbl.configure(text=f"{self.zoom / self.k * 100 / 1.0:.0f}%")

    def set_zoom(self, z, keep_page=True):
        if not self.doc:
            return
        z = max(0.25 * self.k, min(6.0 * self.k, z))
        page = self.current_page()
        self.zoom = z
        self.fit_width = False
        self.images.clear()
        self._relayout()
        self.redraw()
        if keep_page:
            self.goto(page, smooth=False)

    def fit(self):
        if not self.doc:
            return
        cw = self.canvas.winfo_width()
        if cw < 50:
            self.win.after(50, self.fit)
            return
        widest = max(p.rect.width for p in self.doc)
        self.zoom = max(0.25, (cw - 2 * GAP - 4) / widest)
        self.fit_width = True
        self.images.clear()
        self._relayout()
        self.redraw()

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._schedule_render()

    def visible_pages(self) -> list[int]:
        if not self.layout:
            return []
        top = self.canvas.canvasy(0)
        bottom = self.canvas.canvasy(self.canvas.winfo_height())
        return [i for i, (x, y, w, h) in enumerate(self.layout) if y + h >= top - 200 and y <= bottom + 200]

    def current_page(self) -> int:
        if not self.layout:
            return 0
        mid = self.canvas.canvasy(self.canvas.winfo_height() / 3)
        for i, (x, y, w, h) in enumerate(self.layout):
            if y + h + GAP >= mid:
                return i
        return len(self.layout) - 1

    def goto(self, i, smooth=True):
        if not self.layout:
            return
        i = max(0, min(len(self.layout) - 1, i))
        total = float(self.canvas.cget("scrollregion").split()[3]) or 1
        self.canvas.yview_moveto(max(0, self.layout[i][1] - GAP / 2) / total)
        self._schedule_render()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        self.img_items.clear()
        if not self.doc:
            w, h = c.winfo_width(), c.winfo_height()
            c.create_text(w / 2, h / 2 - 14, text="Откройте PDF или перетащите его сюда", fill=TEXT,
                          font=font(15, "bold"))
            c.create_text(w / 2, h / 2 + 16, text="Ctrl+O — открыть", fill=MUTED, font=font(10))
            return
        if not self.layout:
            self._relayout()
        for i, (x, y, w, h) in enumerate(self.layout):
            c.create_rectangle(x + 2, y + 3, x + w + 2, y + h + 3, fill="#C9C9CF", outline="", tags=("shadow",))
            c.create_rectangle(x, y, x + w, y + h, fill="#FFFFFF", outline="#D5D5DA", tags=(f"p{i}",))
            self.img_items[i] = c.create_image(x, y, anchor="nw")
        self._draw_overlays()
        self._schedule_render()

    def _schedule_render(self):
        if self.render_job is None:
            self.render_job = self.win.after(15, self._render_visible)

    def _render_visible(self):
        self.render_job = None
        if not self.doc:
            return
        from PIL import Image, ImageTk
        vis = self.visible_pages()
        for i in vis:
            key = (round(self.zoom, 3), self.page_ver.get(i, 0))
            cached = self.images.get(i)
            if cached is None or cached[0] != key:
                page = self.pg(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False, annots=True)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                self.images[i] = (key, ImageTk.PhotoImage(im))
            if i in self.img_items:
                self.canvas.itemconfigure(self.img_items[i], image=self.images[i][1])
        # освобождаем память от далёких страниц
        for i in list(self.images):
            if i not in vis and len(self.images) > 14:
                self.images.pop(i, None)
        cur = self.current_page()
        self.page_var.set(str(cur + 1))
        self.canvas.tag_raise("overlay")

    # ═════════════════════ координаты ═════════════════════
    def at(self, cx, cy):
        """Точка холста → (страница, точка в координатах PDF) или (None, None)."""
        for i, (x, y, w, h) in enumerate(self.layout):
            if x <= cx <= x + w and y <= cy <= y + h:
                page = self.pg(i)
                vis = fitz.Point((cx - x) / self.zoom, (cy - y) / self.zoom)
                return i, vis * page.derotation_matrix
        return None, None

    def to_canvas(self, pno, rect):
        page = self.pg(pno)
        r = fitz.Rect(rect) * page.rotation_matrix
        r.normalize()
        x, y, _, _ = self.layout[pno]
        return (x + r.x0 * self.zoom, y + r.y0 * self.zoom, x + r.x1 * self.zoom, y + r.y1 * self.zoom)

    def ev_xy(self, e):
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def pt_rect(self, pno, p0, p1, min_size=4):
        r = fitz.Rect(p0, p1)
        r.normalize()
        if r.width < min_size and r.height < min_size:
            return None
        return r

    # ═════════════════════ слова и выделение ═════════════════════
    def words(self, pno):
        if pno not in self.words_cache:
            self.words_cache[pno] = self.pg(pno).get_text("words", sort=True)
        return self.words_cache[pno]

    def word_at(self, pno, pt, nearest=True):
        ws = self.words(pno)
        best, bd = None, 1e9
        for i, w in enumerate(ws):
            r = fitz.Rect(w[:4])
            if r.contains(pt):
                return i
            if nearest:
                dx = max(r.x0 - pt.x, 0, pt.x - r.x1)
                dy = max(r.y0 - pt.y, 0, pt.y - r.y1)
                d = dx + dy * 3
                if d < bd:
                    best, bd = i, d
        return best if bd < 40 else None

    def selected_text(self) -> str:
        out, last = [], None
        for pno, i in self.sel_words:
            w = self.words(pno)[i]
            key = (pno, w[5], w[6])
            if last is not None:
                out.append("\n" if key != last else " ")
            out.append(w[4])
            last = key
        return "".join(out)

    def selection_quads(self):
        """Прямоугольники выделения по строкам: {страница: [Rect]}."""
        lines: dict[tuple, object] = {}
        for pno, i in self.sel_words:
            w = self.words(pno)[i]
            key = (pno, w[5], w[6])
            r = fitz.Rect(w[:4])
            lines[key] = lines[key] | r if key in lines else r
        out: dict[int, list] = {}
        for (pno, _, _), r in lines.items():
            out.setdefault(pno, []).append(r)
        return out

    def _draw_overlays(self):
        c = self.canvas
        c.delete("overlay")
        for pno, rects in self.selection_quads().items():
            for r in rects:
                c.create_rectangle(*self.to_canvas(pno, r), fill=SEL_FILL, stipple="gray25", outline="",
                                   tags=("overlay",))
        for k, (pno, r) in enumerate(self.search_hits):
            cur = k == self.search_i
            c.create_rectangle(*self.to_canvas(pno, r), outline=ACCENT if cur else "#FFB37A",
                               width=3 if cur else 1, tags=("overlay",))
        if self.sel_annot:
            pno, xref = self.sel_annot
            a = self._annot_by_xref(pno, xref)
            if a:
                x0, y0, x1, y1 = self.to_canvas(pno, a.rect)
                c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=SEL_FILL, dash=(4, 3), width=2,
                                   tags=("overlay",))

    def _annot_by_xref(self, pno, xref):
        for a in self.pg(pno).annots() or []:
            if a.xref == xref:
                return a
        return None

    def annot_at(self, pno, pt):
        hit = None
        for a in self.pg(pno).annots() or []:
            r = fitz.Rect(a.rect)
            r = fitz.Rect(r.x0 - 3, r.y0 - 3, r.x1 + 3, r.y1 + 3)
            if r.contains(pt) and a.type[0] != fitz.PDF_ANNOT_POPUP:
                hit = a
        return hit

    def copy(self):
        t = self.selected_text()
        if t:
            self.win.clipboard_clear()
            self.win.clipboard_append(t)
            self.hint(f"Скопировано символов: {len(t)}")

    # ═════════════════════ мышь ═════════════════════
    def _bind(self):
        c = self.canvas
        c.bind("<Configure>", lambda e: (self.fit() if self.fit_width else (self._relayout(), self.redraw())))
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<Button-3>", self.on_context)
        for w in (c, self.win):
            w.bind("<MouseWheel>", self.on_wheel)
            w.bind("<Button-4>", lambda e: self._wheel(e, -1))
            w.bind("<Button-5>", lambda e: self._wheel(e, 1))
        self.win.bind("<KeyPress>", self.on_key)
        self.win.bind("<Delete>", lambda e: self.delete_selected_annot())

    def on_wheel(self, e):
        step = -1 if e.delta > 0 else 1
        return self._wheel(e, step)

    def _wheel(self, e, step):
        if e.state & 0x4:
            self.set_zoom(self.zoom * (1.1 if step < 0 else 1 / 1.1))
        else:
            self.canvas.yview_scroll(step * 3, "units")
            self._schedule_render()
        return "break"

    def on_key(self, e):
        if isinstance(e.widget, tk.Entry) and e.widget is not self.canvas:
            if e.keysym == "Escape":
                self.canvas.focus_set()
            return
        key = shortcut_key(e)
        ctrl = bool(e.state & 0x4)
        if ctrl and key == "s":
            self.save()
        elif ctrl and key == "o":
            self.ask_open()
        elif ctrl and key == "z":
            self.undo()
        elif ctrl and key == "y":
            self.redo()
        elif ctrl and key == "c":
            self.copy()
        elif ctrl and key == "f":
            self.search_entry.focus_set()
        elif ctrl and key == "p":
            self.print_doc()
        elif ctrl and key in ("=", "plus", "equal"):
            self.set_zoom(self.zoom * 1.2)
        elif ctrl and key in ("-", "minus"):
            self.set_zoom(self.zoom / 1.2)
        elif e.keysym == "Escape":
            self.set_tool("select")
            self.sel_words = []
            self.sel_annot = None
            self._draw_overlays()
        elif e.keysym in ("Next", "Prior"):
            self.goto(self.current_page() + (1 if e.keysym == "Next" else -1))
        else:
            return
        return "break"

    def set_tool(self, tid):
        if tid == "sign" and not self.signature_png:
            png = dialogs.get_signature(self.win)
            if not png:
                return
            self.signature_png = png
        if tid == "stamp":
            v = dialogs.ask("stamp", self.win, [])
            if not v:
                return
            self.stamp_choice = pdftools.make_stamp_png(v["text"], pdftools.STAMPS.get(v["text"], (200, 30, 40)),
                                                        v["date"])
        self.tool = tid
        self.canvas.configure(cursor=TOOLS[tid][2])
        self._highlight_tool()
        self.hint(TOOLS[tid][1])

    def _new_signature(self):
        png = dialogs.get_signature(self.win, force_new=True)
        if png:
            self.signature_png = png
            self.set_tool("sign")

    def _highlight_tool(self):
        for tid, b in self.tool_buttons.items():
            on = tid == self.tool
            b.configure(bg=ACCENT if on else CARD, fg="#FFFFFF" if on else TEXT)

    def on_press(self, e):
        self.canvas.focus_set()
        if not self.doc:
            return
        cx, cy = self.ev_xy(e)
        pno, pt = self.at(cx, cy)
        self.drag = {"cx": cx, "cy": cy, "pno": pno, "pt": pt, "moved": False, "path": [(cx, cy)]}
        t = self.tool
        if t == "hand":
            self.canvas.scan_mark(e.x, e.y)
            return
        if pno is None:
            return
        if t == "select":
            a = self.annot_at(pno, pt)
            if a:
                self.sel_annot = (pno, a.xref)
                self.sel_words = []
                self.drag["move_annot"] = (a.xref, fitz.Rect(a.rect))
            else:
                self.sel_annot = None
                i = self.word_at(pno, pt)
                self.drag["word0"] = (pno, i) if i is not None else None
                self.sel_words = []
            self._draw_overlays()
        elif t in ("highlight", "underline", "strike"):
            i = self.word_at(pno, pt)
            self.drag["word0"] = (pno, i) if i is not None else None
        elif t in ("note", "date", "check", "sign", "stamp", "image", "erase", "fill", "edit_text"):
            pass  # действие по отпусканию

    def on_motion(self, e):
        d = self.drag
        if not d or not self.doc:
            return
        cx, cy = self.ev_xy(e)
        d["moved"] = d["moved"] or math.hypot(cx - d["cx"], cy - d["cy"]) > 3
        t = self.tool
        c = self.canvas
        if t == "hand":
            c.scan_dragto(e.x, e.y, gain=1)
            self._schedule_render()
            return
        if d["pno"] is None:
            return
        if t == "select" and d.get("move_annot"):
            c.delete("ghost")
            xref, r = d["move_annot"]
            x0, y0, x1, y1 = self.to_canvas(d["pno"], r)
            dx, dy = cx - d["cx"], cy - d["cy"]
            c.create_rectangle(x0 + dx, y0 + dy, x1 + dx, y1 + dy, outline=SEL_FILL, dash=(4, 3), width=2,
                               tags=("ghost",))
            return
        if t in ("select", "highlight", "underline", "strike") and d.get("word0"):
            pno0, i0 = d["word0"]
            pno, pt = self.at(cx, cy)
            if pno != pno0 or pt is None:
                return
            i1 = self.word_at(pno, pt)
            if i1 is None:
                return
            a, b = sorted((i0, i1))
            self.sel_words = [(pno, i) for i in range(a, b + 1)]
            self._draw_overlays()
            return
        if t == "ink":
            x0, y0 = d["path"][-1]
            d["path"].append((cx, cy))
            c.create_line(x0, y0, cx, cy, fill="#1A2A6C", width=max(2, 2 * self.zoom / 1.3), capstyle="round",
                          tags=("ghost",))
            return
        if t in ("text", "rect", "ellipse", "redact", "f_text", "f_check", "f_combo", "f_sign"):
            c.delete("ghost")
            col = "#000000" if t == "redact" else ("#4C8DFF" if t.startswith("f_") else ACCENT)
            if t == "ellipse":
                c.create_oval(d["cx"], d["cy"], cx, cy, outline=col, width=2, tags=("ghost",))
            else:
                c.create_rectangle(d["cx"], d["cy"], cx, cy, outline=col, width=2, dash=(5, 3) if t != "redact" else None,
                                   fill="#000000" if t == "redact" else "", stipple="gray50" if t == "redact" else "",
                                   tags=("ghost",))
            return
        if t in ("line", "arrow"):
            c.delete("ghost")
            c.create_line(d["cx"], d["cy"], cx, cy, fill=ACCENT, width=2, arrow="last" if t == "arrow" else None,
                          tags=("ghost",))

    def on_release(self, e):
        d, self.drag = self.drag, None
        self.canvas.delete("ghost")
        if not d or not self.doc or self.tool == "hand":
            return
        cx, cy = self.ev_xy(e)
        pno, p0 = d["pno"], d["pt"]
        if pno is None:
            return
        _, p1 = self.at(cx, cy)
        if p1 is None:
            # отпустили за краем страницы — ограничиваем по странице
            x, y, w, h = self.layout[pno]
            p1 = fitz.Point((min(max(cx, x), x + w) - x) / self.zoom, (min(max(cy, y), y + h) - y) / self.zoom)
            p1 = p1 * self.pg(pno).derotation_matrix
        t = self.tool
        try:
            self._apply_tool(t, d, pno, p0, p1)
        except Exception as ex:
            traceback.print_exc()
            self.hint(f"Не получилось: {ex}", error=True)

    def _apply_tool(self, t, d, pno, p0, p1):
        page = self.pg(pno)
        if t == "select":
            if d.get("move_annot") and d["moved"]:
                xref, r = d["move_annot"]
                a = self._annot_by_xref(pno, xref)
                if a:
                    self.checkpoint()
                    dv = (p1 - p0)
                    a.set_rect(fitz.Rect(r.x0 + dv.x, r.y0 + dv.y, r.x1 + dv.x, r.y1 + dv.y))
                    a.update()
                    self.changed([pno])
            elif not d["moved"]:
                self.sel_words = []
                self._draw_overlays()
            else:
                if self.sel_words:
                    self.hint("Ctrl+C — копировать; ПКМ — подсветить/подчеркнуть/зачеркнуть")
            return
        if t in ("highlight", "underline", "strike"):
            if not self.sel_words:
                return
            self.annotate_selection(t)
            return
        if t == "note":
            text = simpledialog.askstring("Заметка", "Текст заметки:", parent=self.win)
            if text:
                self.checkpoint()
                a = page.add_text_annot(p0, text, icon="Comment")
                a.set_info(title=os.environ.get("USERNAME", "Mandarin"))
                a.set_colors(stroke=(1, 0.75, 0.1))
                a.update()
                self.changed([pno])
            return
        if t == "text":
            r = self.pt_rect(pno, p0, p1, 8) or fitz.Rect(p0.x, p0.y, p0.x + 200, p0.y + 40)
            text = ask_multiline(self.win, "Текст", "Введите текст:")
            if text:
                size = max(8, min(28, r.height / max(1, text.count("\n") + 1) / 1.3))
                self.checkpoint()
                pdftools.text_box(page, r, text, size=round(size, 1), color=(0.08, 0.08, 0.12), visible=False)
                self.changed([pno])
            return
        if t == "ink":
            pts = []
            for cx, cy in d["path"]:
                _, pt = self.at(cx, cy)
                if pt is not None:
                    pts.append((pt.x, pt.y))
            if len(pts) >= 2:
                self.checkpoint()
                a = page.add_ink_annot([pts])
                a.set_border(width=1.8)
                a.set_colors(stroke=ANNOT_COLORS["ink"])
                a.update()
                self.changed([pno])
            return
        if t in ("rect", "ellipse"):
            r = self.pt_rect(pno, p0, p1)
            if r:
                self.checkpoint()
                a = page.add_rect_annot(r) if t == "rect" else page.add_circle_annot(r)
                a.set_border(width=1.6)
                a.set_colors(stroke=ANNOT_COLORS["shape"])
                a.update()
                self.changed([pno])
            return
        if t in ("line", "arrow"):
            if abs(p1.x - p0.x) + abs(p1.y - p0.y) > 4:
                self.checkpoint()
                a = page.add_line_annot(p0, p1)
                a.set_border(width=1.6)
                a.set_colors(stroke=ANNOT_COLORS["shape"])
                if t == "arrow":
                    a.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
                a.update()
                self.changed([pno])
            return
        if t == "erase":
            a = self.annot_at(pno, p0)
            if a:
                self.checkpoint()
                page.delete_annot(a)
                self.changed([pno])
            else:
                self.hint("Здесь нет комментария. Добавленный текст, подпись и штамп — часть страницы: Ctrl+Z")
            return
        if t == "sign":
            self.place_png(pno, p0, self.signature_png, width=150)
            return
        if t == "stamp" and self.stamp_choice:
            self.place_png(pno, p0, self.stamp_choice, width=170)
            return
        if t == "image":
            f = filedialog.askopenfilename(parent=self.win, title="Изображение",
                                           filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp *.bmp *.gif")])
            if f:
                from PIL import Image
                import io
                im = Image.open(f)
                im.load()
                buf = io.BytesIO()
                im.convert("RGBA").save(buf, "PNG")
                self.place_png(pno, p0, buf.getvalue(), width=min(240, page.rect.width * 0.5), center=False)
            return
        if t == "date":
            self.checkpoint()
            today = _dt.date.today().strftime("%d.%m.%Y")
            pdftools.text_box(page, (p0.x, p0.y - 8, p0.x + 90, p0.y + 8), today, size=11, color=(0.1, 0.1, 0.4),
                              visible=False)
            self.changed([pno])
            return
        if t == "check":
            self.checkpoint()
            s = 7
            page.draw_polyline([(p0.x - s, p0.y), (p0.x - s * 0.3, p0.y + s * 0.8), (p0.x + s, p0.y - s * 0.9)],
                               color=(0.05, 0.45, 0.2), width=2.2, closePath=False, lineCap=1, lineJoin=1)
            self.changed([pno])
            return
        if t == "fill":
            self.fill_field_at(pno, p0)
            return
        if t == "edit_text":
            self.edit_text_at(pno, p0)
            return
        if t == "redact":
            r = self.pt_rect(pno, p0, p1)
            if r:
                self.checkpoint()
                a = page.add_redact_annot(r, fill=(0, 0, 0))
                a.set_colors(stroke=(0.9, 0.1, 0.1))
                a.update()
                self.changed([pno])
                self.hint("Область отмечена. Нажмите «Применить», чтобы удалить её содержимое навсегда")
            return
        if t.startswith("f_"):
            r = self.pt_rect(pno, p0, p1, 6)
            if not r:
                r = fitz.Rect(p0.x, p0.y, p0.x + (14 if t == "f_check" else 180), p0.y + (14 if t == "f_check" else 20))
            kind = {"f_text": "text", "f_check": "checkbox", "f_combo": "combobox", "f_sign": "signature"}[t]
            existing = {f["name"] for f in pdftools.list_fields(self.doc)}
            n = 1
            while f"Поле {n}" in existing:
                n += 1
            name = simpledialog.askstring("Имя поля", "Имя поля (для заполнения и выгрузки):",
                                          initialvalue=f"Поле {n}", parent=self.win)
            if not name:
                return
            choices = None
            if kind == "combobox":
                raw = simpledialog.askstring("Варианты", "Варианты через точку с запятой:",
                                             initialvalue="Да; Нет", parent=self.win)
                if not raw:
                    return
                choices = [c.strip() for c in raw.split(";") if c.strip()]
            if kind == "checkbox" and r.width > 20:
                r = fitz.Rect(r.x0, r.y0, r.x0 + 14, r.y0 + 14)
            self.checkpoint()
            pdftools.add_field(page, r, kind, name, choices)
            self.changed([pno])
            self.hint(f"Поле «{name}» добавлено. Инструмент «Заполнить» — ввести значения")

    def place_png(self, pno, pt, png, width=150, center=True):
        page = self.pg(pno)
        w, h = pdftools.png_size(png)
        hh = width * h / w
        x0, y0 = (pt.x - width / 2, pt.y - hh / 2) if center else (pt.x, pt.y)
        self.checkpoint()
        pdftools.insert_png(page, fitz.Rect(x0, y0, x0 + width, y0 + hh), png)
        self.changed([pno])

    def annotate_selection(self, kind):
        quads = self.selection_quads()
        if not quads:
            return
        self.checkpoint()
        for pno, rects in quads.items():
            page = self.pg(pno)
            if kind == "highlight":
                a = page.add_highlight_annot(rects)
            elif kind == "underline":
                a = page.add_underline_annot(rects)
            else:
                a = page.add_strikeout_annot(rects)
            a.set_colors(stroke=ANNOT_COLORS[kind])
            a.update()
        pages = list(quads)
        self.sel_words = []
        self.changed(pages)

    def on_double(self, e):
        if not self.doc:
            return
        pno, pt = self.at(*self.ev_xy(e))
        if pno is None:
            return
        a = self.annot_at(pno, pt)
        if a and a.type[0] in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FREE_TEXT):
            text = simpledialog.askstring("Заметка", "Текст:", initialvalue=a.info.get("content", ""), parent=self.win)
            if text is not None:
                self.checkpoint()
                a.set_info(content=text)
                a.update()
                self.changed([pno])
            return
        if self.tool == "select":
            i = self.word_at(pno, pt, nearest=False)
            if i is not None:
                w = self.words(pno)[i]
                self.sel_words = [(pno, j) for j, x in enumerate(self.words(pno)) if (x[5], x[6]) == (w[5], w[6])]
                self._draw_overlays()

    def on_context(self, e):
        if not self.doc:
            return
        cx, cy = self.ev_xy(e)
        pno, pt = self.at(cx, cy)
        m = tk.Menu(self.win, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground="#FFFFFF",
                    font=font(10))
        if self.sel_words:
            m.add_command(label="Копировать", command=self.copy)
            m.add_command(label="Подсветить маркером", command=lambda: self.annotate_selection("highlight"))
            m.add_command(label="Подчеркнуть", command=lambda: self.annotate_selection("underline"))
            m.add_command(label="Зачеркнуть", command=lambda: self.annotate_selection("strike"))
            m.add_command(label="Зачернить выделенное", command=self.redact_selection)
            m.add_separator()
        if pno is not None:
            a = self.annot_at(pno, pt)
            if a:
                self.sel_annot = (pno, a.xref)
                self._draw_overlays()
                m.add_command(label="Удалить комментарий", command=self.delete_selected_annot)
                m.add_separator()
            m.add_command(label="Повернуть страницу", command=lambda: self.rotate_page(pno))
            m.add_command(label="Удалить страницу", command=lambda: self.delete_page(pno))
            m.add_command(label="Добавить закладку сюда", command=lambda: self.add_bookmark(pno))
        m.tk_popup(e.x_root, e.y_root)

    def delete_selected_annot(self):
        if not self.sel_annot:
            return
        pno, xref = self.sel_annot
        a = self._annot_by_xref(pno, xref)
        if a:
            self.checkpoint()
            self.pg(pno).delete_annot(a)
            self.sel_annot = None
            self.changed([pno])

    def redact_selection(self):
        quads = self.selection_quads()
        if not quads:
            return
        self.checkpoint()
        for pno, rects in quads.items():
            for r in rects:
                self.pg(pno).add_redact_annot(r, fill=(0, 0, 0))
        self.sel_words = []
        self.changed(list(quads))
        self.hint("Отмечено для зачернения. «Применить» — удалить навсегда")

    # ═════════════════════ формы и правка текста ═════════════════════
    def fill_field_at(self, pno, pt):
        page = self.pg(pno)
        for w in page.widgets():
            if fitz.Rect(w.rect).contains(pt):
                if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    self.checkpoint()
                    w.field_value = "Off" if w.field_value not in ("Off", "", False, None) else w.on_state()
                    w.update()
                    self.changed([pno])
                elif w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                    m = tk.Menu(self.win, tearoff=0, font=font(10))
                    for ch in w.choice_values or []:
                        val = ch[1] if isinstance(ch, (list, tuple)) else ch
                        m.add_command(label=val, command=lambda v=val, w=w: self._set_widget(pno, w, v))
                    m.tk_popup(self.win.winfo_pointerx(), self.win.winfo_pointery())
                elif w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
                    if not self.signature_png:
                        self.signature_png = dialogs.get_signature(self.win)
                    if self.signature_png:
                        r = fitz.Rect(w.rect)
                        self.checkpoint()
                        pdftools.insert_png(page, r, self.signature_png)
                        self.changed([pno])
                else:
                    self._inline_entry(pno, w)
                return
        self.hint("Щёлкните по полю формы. Полей нет? Создайте их в разделе «Формы»")

    def _set_widget(self, pno, w, value):
        self.checkpoint()
        w.field_value = value
        w.update()
        self.changed([pno])

    def _inline_entry(self, pno, w):
        x0, y0, x1, y1 = self.to_canvas(pno, w.rect)
        multiline = bool(w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE)
        if multiline:
            val = ask_multiline(self.win, w.field_name, "Значение:", w.field_value or "")
            if val is not None:
                self._set_widget(pno, w, val)
            return
        var = tk.StringVar(value=w.field_value or "")
        e = tk.Entry(self.canvas, textvariable=var, font=font(max(8, int((y1 - y0) / 2.2 / self.k))), bd=1,
                     relief="solid", bg="#FFFDE7")
        win_id = self.canvas.create_window(x0, y0, window=e, anchor="nw", width=x1 - x0, height=y1 - y0)
        e.focus_set()
        e.select_range(0, "end")
        xref = w.xref
        done = {"v": False}

        def commit(_=None):
            if done["v"]:
                return
            done["v"] = True
            val = var.get()
            self.canvas.delete(win_id)
            e.destroy()
            for ww in self.pg(pno).widgets():
                if ww.xref == xref and (ww.field_value or "") != val:
                    self._set_widget(pno, ww, val)
                    break
        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Escape>", lambda ev: (done.__setitem__("v", True), self.canvas.delete(win_id), e.destroy()))

    def edit_text_at(self, pno, pt):
        page = self.pg(pno)
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                if not fitz.Rect(line["bbox"]).contains(pt):
                    continue
                spans = line["spans"]
                old = "".join(s["text"] for s in spans)
                bbox = fitz.Rect(line["bbox"])
                s0 = max(spans, key=lambda s: len(s["text"]))
                new = ask_multiline(self.win, "Изменить текст", "Строка будет заменена новым текстом:", old)
                if new is None or new == old:
                    return
                size = s0["size"]
                col = s0["color"]
                rgb = ((col >> 16) & 255, (col >> 8) & 255, col & 255)
                bold = bool(s0["flags"] & 16) or "bold" in s0["font"].lower()
                serif = bool(s0["flags"] & 4) or any(x in s0["font"].lower() for x in ("times", "serif", "roman"))
                self.checkpoint()
                page.add_redact_annot(bbox)
                try:
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                          graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0))
                except TypeError:   # старые PyMuPDF без параметра graphics
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                width = max(bbox.width, pdftools._text_width(new, size, bold) + 4)
                r = fitz.Rect(bbox.x0, bbox.y0 - 1, min(page.rect.width - 4, bbox.x0 + width + size),
                              bbox.y1 + size * (new.count("\n")) + 2)
                pdftools.text_box(page, r, new, size=round(size, 1), color=tuple(c / 255 for c in rgb), bold=bold,
                                  font="serif" if serif else "sans-serif", visible=False)
                self.changed([pno])
                self.hint("Текст изменён. Шрифт подобран похожий — проверьте результат (Ctrl+Z — отменить)")
                return
        self.hint("Под курсором нет текста. Для сканов сначала «Распознать текст (OCR)»")

    # ═════════════════════ страницы и закладки ═════════════════════
    def rotate_page(self, pno=None):
        if not self.doc:
            return
        pno = self.current_page() if pno is None else pno
        self.checkpoint()
        p = self.pg(pno)
        p.set_rotation((p.rotation + 90) % 360)
        self.changed()

    def delete_page(self, pno=None):
        if not self.doc or len(self.doc) < 2:
            return
        pno = self.current_page() if pno is None else pno
        if not messagebox.askyesno("Удалить страницу", f"Удалить страницу {pno + 1}?", parent=self.win):
            return
        self.checkpoint()
        self.doc.delete_page(pno)
        self._reset_caches()
        self.changed()
        self._load_toc()

    def _load_toc(self):
        self.toc_list.delete(0, "end")
        if not self.doc:
            return
        self.toc = self.doc.get_toc()
        for lvl, title, page in self.toc:
            self.toc_list.insert("end", "   " * (lvl - 1) + f"{title}  · {page}")
        if not self.toc:
            self.toc_list.insert("end", "Закладок нет")

    def _toc_jump(self, _e):
        sel = self.toc_list.curselection()
        if sel and getattr(self, "toc", None) and sel[0] < len(self.toc):
            self.goto(self.toc[sel[0]][2] - 1)

    def add_bookmark(self, pno=None):
        if not self.doc:
            return
        pno = self.current_page() if pno is None else pno
        title = simpledialog.askstring("Закладка", "Название закладки:", initialvalue=f"Страница {pno + 1}",
                                       parent=self.win)
        if not title:
            return
        self.checkpoint()
        toc = self.doc.get_toc()
        toc.append([1, title, pno + 1])
        toc.sort(key=lambda t: t[2]) if all(t[0] == 1 for t in toc) else None
        self.doc.set_toc(toc)
        self.dirty = True
        self._update_title()
        self._load_toc()

    def del_bookmark(self):
        sel = self.toc_list.curselection()
        if not sel or not getattr(self, "toc", None) or sel[0] >= len(self.toc):
            return
        self.checkpoint()
        toc = self.doc.get_toc()
        del toc[sel[0]]
        self.doc.set_toc(toc)
        self.dirty = True
        self._update_title()
        self._load_toc()

    # ═════════════════════ поиск ═════════════════════
    def search(self, step):
        if not self.doc:
            return
        q = self.search_var.get().strip()
        if not q:
            return
        if getattr(self, "_last_q", None) != q:
            self._last_q = q
            self.search_hits = [(i, r) for i, p in enumerate(self.doc) for r in p.search_for(q)]
            self.search_i = -1
        if not self.search_hits:
            self.search_lbl.configure(text="не найдено")
            self._draw_overlays()
            return
        self.search_i = (self.search_i + step) % len(self.search_hits)
        pno, r = self.search_hits[self.search_i]
        self.search_lbl.configure(text=f"{self.search_i + 1} из {len(self.search_hits)}")
        x0, y0, x1, y1 = self.to_canvas(pno, r)
        total = float(self.canvas.cget("scrollregion").split()[3]) or 1
        self.canvas.yview_moveto(max(0, y0 - self.canvas.winfo_height() / 3) / total)
        self._schedule_render()
        self._draw_overlays()

    # ═════════════════════ функции документа ═════════════════════
    def find_redact(self):
        if not self.doc:
            return
        v = dialogs.ask("redact", self.win, [self.path or Path("документ.pdf")])
        if not v:
            return
        hits = pdftools.find_sensitive(self.doc, v["keys"], v["words"], v["regex"])
        if not hits:
            self.hint("Ничего не найдено")
            return
        self.checkpoint()
        for pno, r in hits:
            a = self.pg(pno).add_redact_annot(r, fill=(0, 0, 0))
            a.set_colors(stroke=(0.9, 0.1, 0.1))
            a.update()
        self.changed(sorted({p for p, _ in hits}))
        self.hint(f"Найдено фрагментов: {len(hits)} — проверьте красные рамки и нажмите «Применить»")

    def apply_redact(self):
        if not self.doc:
            return
        pages = [i for i in range(len(self.doc))
                 if any(a.type[0] == fitz.PDF_ANNOT_REDACT for a in (self.pg(i).annots() or []))]
        if not pages:
            self.hint("Нет отмеченных областей. Инструмент «Зачернить» или «Найти…»")
            return
        if not messagebox.askyesno("Применить зачернение", "Отмеченное содержимое будет удалено из документа "
                                   "безвозвратно (после сохранения). Продолжить?", parent=self.win):
            return
        self.checkpoint()
        for i in pages:
            self.pg(i).apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        self.doc.set_metadata({})
        self.changed()
        self.hint("Зачернение применено. Сохраните документ")

    def _doc_op(self, key, fn):
        if not self.doc:
            return
        v = dialogs.ask(key, self.win, [self.path or Path("документ.pdf")])
        if v is None:
            return
        self.checkpoint()
        try:
            fn(v)
        except Exception as e:
            self.undo_stack.pop()
            self.hint(f"Не получилось: {e}", error=True)
            return
        self.changed()

    def do_watermark(self):
        self._doc_op("watermark", lambda v: pdftools.watermark_doc(self.doc, **v))

    def do_numbers(self):
        self._doc_op("numbers", lambda v: pdftools.page_numbers_doc(self.doc, **v))

    def do_header_footer(self):
        self._doc_op("header_footer", lambda v: pdftools.header_footer_doc(
            self.doc, v["header"], v["footer"], file_name=self.path.name if self.path else ""))

    def do_crop(self):
        self._doc_op("crop", lambda v: pdftools.crop_doc(self.doc, **v))

    def do_flatten(self):
        if not self.doc:
            return
        if not messagebox.askyesno("Сгладить", "Формы и комментарии станут частью страниц — их больше нельзя "
                                   "будет редактировать. Продолжить?", parent=self.win):
            return
        self.checkpoint()
        pdftools.flatten_doc(self.doc)
        self.changed()

    def do_ocr(self):
        if not self.doc:
            return
        if not pdftools.ocr_available():
            self.hint("Нет языковых файлов OCR — папка tessdata рядом с программой", error=True)
            return
        self._run_file_job("ocr", lambda src: pdftools.ocr_file(src, progress=self._progress), reload=True)

    def _progress(self, frac):
        self.jobs.put(("progress", frac))

    def open_organizer(self):
        if not self.doc or not self.on_open_organizer:
            return
        tmp = self._temp_copy()
        self.on_open_organizer([tmp])
        self.hint("Страницы открыты в редакторе порядка. Сохраните там результат и откройте его здесь")

    def _temp_copy(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="mandarin-"))
        p = d / (self.path.name if self.path else "документ.pdf")
        if p.suffix.lower() != ".pdf":
            p = p.with_suffix(".pdf")
        p.write_bytes(self.doc.tobytes(garbage=3, deflate=True))
        return p

    def file_op(self, op):
        """Операции, которые создают новый файл рядом с оригиналом."""
        if not self.doc:
            return
        fake = [self.path or Path("документ.pdf")]
        if op == "unlock" and self.password:
            v = {}      # документ уже открыт с паролем — сохраним копию без защиты
        elif op in ("protect", "unlock", "compare", "sign_cert"):
            v = dialogs.ask(op, self.win, fake if op != "compare" else [self._temp_copy()])
            if v is None:
                return
        else:
            v = {}
        if op == "compare" and self.path:
            # сравниваем текущую (возможно изменённую) версию с выбранным файлом
            v = {"a": v["a"], "b": v["b"]}
        funcs = {
            "protect": lambda s: pdftools.protect(s, **v),
            "unlock": lambda s: pdftools.unlock(s, v["password"]) if self.password is None else
            engine.Result([_copy_plain(s)], "Защита снята"),
            "compare": lambda s: pdftools.compare_files(v["a"], v["b"]),
            "sign_cert": lambda s: pdftools.sign_with_certificate(s, **v),
            "verify": pdftools.verify_file,
            "pdfa": pdftools.to_pdfa,
            "docx": lambda s: engine.pdf_to_docx([s]),
            "xlsx": lambda s: pdftools.export_xlsx([s]),
            "pptx": lambda s: pdftools.export_pptx([s]),
        }
        if op == "pdfa" and not pdftools.pdfa_available():
            messagebox.showinfo("PDF/A", "Для PDF/A нужен бесплатный Ghostscript.\n"
                                "Установите его с ghostscript.com (или winget install ArtifexSoftware.GhostScript) "
                                "и повторите.", parent=self.win)
            return
        self._run_file_job(op, funcs[op], reload=False, open_result=op in ("compare", "sign_cert"))

    def _run_file_job(self, name, fn, reload=False, open_result=False):
        src = self._temp_copy()
        out_dir = self.path.parent if self.path and os.access(self.path.parent, os.W_OK) else Path.home()
        self.hint("Выполняю…")
        self.win.configure(cursor="watch")

        def work():
            try:
                res = fn(src)
                moved = []
                tmp_root = Path(tempfile.gettempdir()).resolve()
                for o in res.outputs:
                    o = Path(o)
                    if tmp_root in o.resolve().parents:      # результат во временной папке → рядом с оригиналом
                        dst = engine.unique(out_dir / o.name)
                        shutil.move(str(o), dst)
                        moved.append(dst)
                    else:
                        moved.append(o)
                res.outputs = moved
                self.jobs.put(("done", name, res, reload, open_result))
            except engine.EngineError as e:
                self.jobs.put(("error", name, str(e)))
            except Exception as e:
                traceback.print_exc()
                self.jobs.put(("error", name, f"{type(e).__name__}: {e}"))
            finally:
                shutil.rmtree(src.parent, ignore_errors=True)
        threading.Thread(target=work, daemon=True).start()

    def _poll_jobs(self):
        try:
            while True:
                ev = self.jobs.get_nowait()
                if ev[0] == "progress":
                    self.status.configure(text=f"Распознаю… {int(ev[1] * 100)}%", fg=ACCENT)
                    continue
                self.win.configure(cursor="")
                if ev[0] == "error":
                    self.hint(f"Не получилось: {ev[2]}", error=True)
                    continue
                _, name, res, reload, open_result = ev
                out = res.outputs[0] if res.outputs else None
                if reload and out:
                    self.checkpoint()
                    self.open(out, keep_path=True)
                    self.dirty = True
                    self._update_title()
                    out.unlink(missing_ok=True)
                    self.done("Готово ✓", res.message or "")
                    continue
                if out:
                    self.done("Готово ✓", f"{out.name}" + (f" · {res.message}" if res.message else ""), out)
                    if open_result:
                        PdfViewer(self.master, out, self.on_done, self.on_open_organizer)
                else:
                    self.done("Готово ✓", res.message or "")
                    if name == "verify":
                        messagebox.showinfo("Подписи", res.message, parent=self.win)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        try:
            self.win.after(100, self._poll_jobs)
        except tk.TclError:
            pass

    def _dnd_drop(self, e):
        files = [Path(f) for f in self.win.tk.splitlist(e.data)]
        if files:
            self.win.after(10, lambda: (self._confirm_discard() and self.open(files[0])))
        return "copy"


def _copy_plain(src: Path) -> Path:
    dst = engine.target(src, ".pdf", " (без пароля)")
    shutil.copy(src, dst)
    return dst


def ask_multiline(parent, title, prompt, initial="") -> str | None:
    """Небольшое окно с многострочным полем ввода."""
    k = ui_scale(parent)
    res = {"v": None}
    w = tk.Toplevel(parent)
    w.title(title)
    w.configure(bg=PANEL)
    box = tk.Frame(w, bg=PANEL, padx=int(18 * k), pady=int(14 * k))
    box.pack(fill="both", expand=True)
    tk.Label(box, text=prompt, bg=PANEL, fg=TEXT, font=font(10), anchor="w").pack(fill="x")
    t = tk.Text(box, width=48, height=5, font=font(11), wrap="word", bd=1, relief="solid", bg="#FFFFFF", fg=TEXT)
    t.pack(fill="both", expand=True, pady=(int(6 * k), int(10 * k)))
    t.insert("1.0", initial)
    bar = tk.Frame(box, bg=PANEL)
    bar.pack(fill="x")

    def ok(_=None):
        res["v"] = t.get("1.0", "end-1c")
        w.destroy()
        return "break"
    FlatButton(bar, "Готово", ok, kind="accent").pack(side="right")
    FlatButton(bar, "Отмена", w.destroy).pack(side="right", padx=(0, int(8 * k)))
    tk.Label(bar, text="Ctrl+Enter — готово", bg=PANEL, fg=MUTED, font=font(8)).pack(side="left")
    t.bind("<Control-Return>", ok)
    w.bind("<Escape>", lambda e: w.destroy())
    w.transient(parent)
    w.update_idletasks()
    w.geometry(f"+{parent.winfo_rootx() + 120}+{parent.winfo_rooty() + 120}")
    t.focus_set()
    t.tag_add("sel", "1.0", "end")
    try:
        w.grab_set()
    except tk.TclError:
        pass
    w.wait_window()
    return res["v"]


def main(argv):
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    root.withdraw()
    v = PdfViewer(root, Path(argv[0]) if argv else None)
    v.win.bind("<Destroy>", lambda e: e.widget is v.win and root.destroy())
    root.mainloop()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
