"""
Mandarin — главное окно: перетащите файлы (или выберите их) и нажмите нужное действие.
Работает и без жеста Shift — надёжный способ конвертировать что угодно.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog

import engine
import license as lic
from ui_kit import (ACCENT, BG, CARD, CARD_HOVER, DANGER, GREEN, IS_WIN, LINE, MUTED, PANEL, TEXT, FlatButton,
                    dnd_available, font, ui_scale)

CAT_NAMES = {"image": "Изображения", "audio": "Аудио", "video": "Видео", "pdf": "PDF",
             "doc": "Документы", "folder": "Папки", "other": "Файлы", "mixed": "Разные файлы"}


def reveal(path: Path):
    if not IS_WIN:
        return
    if path.is_dir():
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["explorer", "/select,", str(path)])


class Studio:
    def __init__(self, master, open_organizer, set_clipboard=None, log_error=None, open_viewer=None, ask=None):
        self.k = k = ui_scale(master)
        self.open_organizer = open_organizer
        self.open_viewer = open_viewer
        self.ask = ask
        self.set_clipboard = set_clipboard
        self.log_error = log_error or (lambda t: None)
        self.files: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.busy = 0

        w = self.win = tk.Toplevel(master)
        w.title("Mandarin — конвертер файлов")
        w.configure(bg=BG)
        w.geometry(f"{int(900 * k)}x{int(680 * k)}")
        w.minsize(int(640 * k), int(520 * k))
        try:
            from ui_kit import resource_path
            w.iconbitmap(resource_path("mandarin.ico"))
        except Exception:
            pass

        head = tk.Frame(w, bg=BG, padx=int(24 * k), pady=int(18 * k))
        head.pack(fill="x")
        tk.Label(head, text="🍊 Mandarin", bg=BG, fg=TEXT, font=font(20, "bold")).pack(side="left")
        FlatButton(head, "Упорядочить PDF", lambda: self.open_organizer(self._pdf_like()),
                   kind="normal", size=10).pack(side="right")
        if open_viewer:
            FlatButton(head, "PDF-редактор", lambda: self.open_viewer(self._pdf_like()),
                       kind="accent", size=10).pack(side="right", padx=(0, int(8 * k)))
        self.lic_lbl = tk.Label(head, text="", bg=BG, fg=ACCENT, font=font(9, "underline"), cursor="hand2")
        self.lic_lbl.pack(side="right", padx=(0, int(12 * k)))
        self.lic_lbl.bind("<Button-1>", lambda e: self._license_dialog())
        tk.Label(head, text="конвертер файлов — офлайн", bg=BG, fg=MUTED, font=font(10)
                 ).pack(side="left", padx=int(12 * k), pady=(int(8 * k), 0))

        # зона перетаскивания
        self.drop = tk.Canvas(w, bg=PANEL, highlightthickness=0, height=int(130 * k), cursor="hand2")
        self.drop.pack(fill="x", padx=int(24 * k))
        self.drop.bind("<Configure>", lambda e: self._draw_drop())
        self.drop.bind("<Button-1>", lambda e: self.ask_files())
        if dnd_available(w):
            from tkinterdnd2 import DND_FILES
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<DropEnter>>", lambda e: (self._draw_drop(True), "copy")[1])
            self.drop.dnd_bind("<<DropLeave>>", lambda e: (self._draw_drop(False), e.action)[1])
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        # действия
        # журнал внизу (пакуем раньше прокручиваемой области, чтобы он не уезжал)
        self.log = tk.Frame(w, bg=PANEL, padx=int(16 * k), pady=int(8 * k))
        self.log.pack(fill="x", side="bottom")

        # действия — в прокручиваемой области (для PDF их больше 30)
        holder = tk.Frame(w, bg=BG)
        holder.pack(fill="both", expand=True)
        self.scroll = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(holder, orient="vertical", command=self.scroll.yview)
        self.scroll.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.scroll.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.scroll, bg=BG, padx=int(24 * k), pady=int(12 * k))
        self._body_id = self.scroll.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.scroll.configure(scrollregion=self.scroll.bbox("all")))
        self.scroll.bind("<Configure>", lambda e: self.scroll.itemconfigure(self._body_id, width=e.width))
        for wdg in (self.scroll, w):
            wdg.bind("<MouseWheel>", lambda e: self.scroll.yview_scroll(int(-e.delta / 120) * 2, "units"))
            wdg.bind("<Button-4>", lambda e: self.scroll.yview_scroll(-2, "units"))
            wdg.bind("<Button-5>", lambda e: self.scroll.yview_scroll(2, "units"))

        # журнал
        self.log_lines: list[tk.Widget] = []
        self._log("Перетащите файлы в окно. В Проводнике: начните перетаскивать файл и нажмите Shift — "
                  "колесо форматов, Ctrl — инструменты.", MUTED)

        self.refresh_license()
        self._drain()

    # ── файлы ──
    def _draw_drop(self, hot=False):
        c = self.drop
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        k = self.k
        c.configure(bg="#FFF1E8" if hot else PANEL)
        c.create_rectangle(6, 6, w - 6, h - 6, outline=ACCENT if hot else LINE, dash=(6, 4), width=2)
        if not self.files:
            c.create_text(w / 2, h / 2 - 12 * k, text="Перетащите файлы сюда", fill=TEXT, font=font(14, "bold"))
            c.create_text(w / 2, h / 2 + 14 * k, text="или нажмите, чтобы выбрать · картинки, PDF, аудио, видео, документы",
                          fill=MUTED, font=font(9))
        else:
            names = ", ".join(p.name for p in self.files[:3]) + (f" и ещё {len(self.files) - 3}" if len(self.files) > 3 else "")
            cat = CAT_NAMES.get(engine.common_category(self.files), "Файлы")
            c.create_text(w / 2, h / 2 - 12 * k, text=f"{cat}: {len(self.files)}", fill=TEXT, font=font(13, "bold"))
            c.create_text(w / 2, h / 2 + 12 * k, text=names, fill=MUTED, font=font(9), width=w - 60)
            c.create_text(w - 18, 18, text="очистить ✕", fill=MUTED, font=font(9), anchor="ne", tags=("clear",))
            c.tag_bind("clear", "<Button-1>", lambda e: (self.set_files([]), "break")[1])

    def _on_drop(self, e):
        files = [Path(f) for f in self.win.tk.splitlist(e.data)]
        self.win.after(10, lambda: self.set_files(files))
        return "copy"

    def ask_files(self):
        files = filedialog.askopenfilenames(parent=self.win, title="Выберите файлы")
        if files:
            self.set_files([Path(f) for f in files])

    def set_files(self, files: list[Path]):
        self.files = [f for f in files if f.exists()]
        self._draw_drop()
        self.render_actions()

    def _pdf_like(self):
        return [p for p in self.files if engine.category(p) in ("pdf", "image")]

    # ── кнопки действий ──
    def render_actions(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.scroll.yview_moveto(0)
        k = self.k
        if not self.files:
            tk.Label(self.body, text="Что умеет Mandarin", bg=BG, fg=TEXT, font=font(12, "bold"), anchor="w").pack(fill="x")
            for t in ["Картинки: JPG, PNG, WEBP, HEIC, GIF, BMP, TIFF, ICO, PDF · сжать, уменьшить, повернуть, убрать EXIF, QR",
                      "PDF: в Word (DOCX), PNG/JPG, текст · объединить, разделить, сжать, повернуть, упорядочить страницы",
                      "Аудио и видео: MP3, M4A, WAV, FLAC, OGG, OPUS, MP4, MOV, WEBM, MKV, GIF · сжать, без звука",
                      "Документы Word/Excel/PowerPoint → PDF и обратно (если установлен MS Office или LibreOffice)"]:
                tk.Label(self.body, text="•  " + t, bg=BG, fg=MUTED, font=font(10), anchor="w", justify="left",
                         wraplength=int(820 * k)).pack(fill="x", pady=int(3 * k))
            return
        convert = engine.actions_for(self.files, "convert")
        tools = engine.actions_for(self.files, "tools")
        cat = engine.common_category(self.files)
        self._section("Конвертировать в", convert, ACCENT)
        if cat == "pdf":
            groups = [("PDF Pro", {"pdf_open", "pdf_sign", "pdf_protect", "pdf_unlock", "pdf_ocr", "pdf_redact",
                                   "pdf_cert", "pdf_verify", "pdf_compare"}),
                      ("Оформление и страницы", {"pdf_watermark", "pdf_numbers", "pdf_headers", "pdf_bates", "pdf_crop",
                                                 "organize", "pdf_merge", "pdf_split", "pdf_rotate", "pdf_flatten"})]
            used = set()
            for title, ids in groups:
                part = [a for a in tools if a.id in ids]
                used |= {a.id for a in part}
                self._section(title, part, GREEN)
            self._section("Ещё", [a for a in tools if a.id not in used], GREEN)
        else:
            self._section("Инструменты", tools, GREEN)
        if cat == "doc" and not convert:
            tk.Label(self.body, text="Чтобы конвертировать документы Word/Excel/PowerPoint, нужен Microsoft Office "
                     "или бесплатный LibreOffice (libreoffice.org). PDF → Word работает без них.",
                     bg=BG, fg=ACCENT, font=font(10), wraplength=int(820 * k), justify="left", anchor="w"
                     ).pack(fill="x", pady=(int(10 * k), 0))

    def refresh_license(self):
        if lic.FREE_BUILD or lic.is_pro():
            self.lic_lbl.configure(text="")
        else:
            self.lic_lbl.configure(text=f"Пробный Pro: {lic.trial_days_left()} дн. · Купить"
                                   if lic.trial_days_left() else "Купить Pro")
        self.render_actions()

    def _license_dialog(self):
        import license_ui
        license_ui.LicenseDialog(self.win, on_change=lambda _l: self.refresh_license())

    def _section(self, title, actions, color):
        if not actions:
            return
        k = self.k
        tk.Label(self.body, text=title.upper(), bg=BG, fg=MUTED, font=font(8, "bold"), anchor="w"
                 ).pack(fill="x", pady=(int(10 * k), int(6 * k)))
        grid = tk.Frame(self.body, bg=BG)
        grid.pack(fill="x")
        cols = 5
        for i, a in enumerate(actions):
            cell = tk.Frame(grid, bg=CARD, cursor="hand2", padx=int(10 * k), pady=int(8 * k))
            cell.grid(row=i // cols, column=i % cols, sticky="nsew", padx=(0, int(8 * k)), pady=(0, int(8 * k)))
            locked = lic.needs_pro(a)
            t1 = tk.Label(cell, text=a.label + ("  🔒" if locked else ""), bg=CARD,
                          fg=MUTED if locked else (color if a.ui is None else TEXT), font=font(12, "bold"), anchor="w")
            t1.pack(fill="x")
            t2 = tk.Label(cell, text=a.hint, bg=CARD, fg=MUTED, font=font(8), anchor="w", justify="left",
                          wraplength=int(138 * k))
            t2.pack(fill="x")
            for wdg in (cell, t1, t2):
                wdg.bind("<Button-1>", lambda e, a=a: self.run(a))
                wdg.bind("<Enter>", lambda e, ws=(cell, t1, t2): [x.configure(bg=CARD_HOVER) for x in ws])
                wdg.bind("<Leave>", lambda e, ws=(cell, t1, t2): [x.configure(bg=CARD) for x in ws])
        for c in range(cols):
            grid.grid_columnconfigure(c, weight=1, uniform="a")

    # ── выполнение ──
    def run(self, action: engine.Action):
        import license_ui
        files = list(self.files)
        if not license_ui.require_pro(self.win, action, on_change=lambda _l: self.refresh_license()):
            return
        if action.ui == "organizer":
            self.open_organizer([p for p in files if engine.category(p) in ("pdf", "image")])
            return
        if action.ui and action.ui.startswith("viewer") and self.open_viewer:
            self.open_viewer(files, *([action.ui.split(":", 1)[1]] if ":" in action.ui else []))
            return
        params = self.ask(action, files) if (action.ask and self.ask) else {}
        if params is None:
            return
        self.busy += 1
        self._log(f"⏳ {action.hint}: {files[0].name}" + (f" и ещё {len(files) - 1}" if len(files) > 1 else ""), MUTED)
        self.win.configure(cursor="watch")

        def work():
            try:
                self.events.put(("ok", action, action.run(files, **params)))
            except engine.EngineError as e:
                self.events.put(("err", action, str(e)))
            except Exception as e:
                self.log_error(traceback.format_exc())
                self.events.put(("err", action, f"{type(e).__name__}: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, action, payload = self.events.get_nowait()
                self.busy -= 1
                if kind == "ok":
                    res = payload
                    if res.clipboard is not None and self.set_clipboard:
                        self.set_clipboard(res.clipboard)
                    if res.outputs:
                        first = res.outputs[0]
                        more = f" и ещё {len(res.outputs) - 1}" if len(res.outputs) > 1 else ""
                        self._log(f"✓ {first.name}{more}" + (f"  ·  {res.message}" if res.message else ""),
                                  TEXT, link=first)
                    else:
                        self._log(f"✓ {res.message or action.hint}", TEXT)
                else:
                    self._log(f"✕ {action.hint}: {payload}", DANGER)
        except queue.Empty:
            pass
        if self.busy <= 0:
            self.busy = 0
            try:
                self.win.configure(cursor="")
            except tk.TclError:
                return
        self.win.after(80, self._drain)

    def _log(self, text, color, link: Path | None = None):
        row = tk.Frame(self.log, bg=PANEL)
        row.pack(fill="x", side="top")
        tk.Label(row, text=text, bg=PANEL, fg=color, font=font(9), anchor="w").pack(side="left")
        if link is not None:
            a = tk.Label(row, text="Показать в папке", bg=PANEL, fg=ACCENT, font=font(9, "underline"), cursor="hand2")
            a.pack(side="right")
            a.bind("<Button-1>", lambda e: reveal(link))
        self.log_lines.append(row)
        while len(self.log_lines) > 4:
            self.log_lines.pop(0).destroy()

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
