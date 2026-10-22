"""
Mandarin — диалоги параметров для функций PDF Pro и панель подписи мышью.

ask(key, parent, files) → dict с параметрами или None (если пользователь отменил).
Последние введённые значения запоминаются в %APPDATA%\\Mandarin\\dialogs.json (кроме паролей).
"""
from __future__ import annotations

import io
import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import pdftools
from ui_kit import (ACCENT, CARD, LINE, MUTED, PANEL, TEXT, FlatButton, font, ui_scale)


def store_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "Mandarin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memory() -> dict:
    try:
        return json.loads((store_dir() / "dialogs.json").read_text("utf-8"))
    except Exception:
        return {}


def _remember(key: str, values: dict):
    mem = _memory()
    mem[key] = {k: v for k, v in values.items() if "password" not in k and "pw" not in k}
    try:
        (store_dir() / "dialogs.json").write_text(json.dumps(mem, ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass


# ───────────────────────── конструктор форм ─────────────────────────

class FormDialog:
    """Модальное окно с полями. fields: [{key, label, type, default, options, hint}]."""

    def __init__(self, parent, title: str, subtitle: str, fields: list[dict], ok_text="Готово",
                 memory_key: str | None = None, extra_button=None):
        self.k = k = ui_scale(parent)
        self.result = None
        self.fields = fields
        self.vars: dict[str, tk.Variable] = {}
        mem = _memory().get(memory_key, {}) if memory_key else {}
        self.memory_key = memory_key

        w = self.win = tk.Toplevel(parent)
        w.title(title)
        w.configure(bg=PANEL)
        w.resizable(False, False)
        w.transient(parent if parent.winfo_viewable() else None)
        pad = int(22 * k)
        box = tk.Frame(w, bg=PANEL, padx=pad, pady=int(18 * k))
        box.pack(fill="both", expand=True)
        tk.Label(box, text=title, bg=PANEL, fg=TEXT, font=font(14, "bold"), anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(box, text=subtitle, bg=PANEL, fg=MUTED, font=font(9), anchor="w", justify="left",
                     wraplength=int(400 * k)).pack(fill="x", pady=(int(2 * k), int(10 * k)))
        first_entry = None
        for f in fields:
            key, kind = f["key"], f.get("type", "text")
            default = mem.get(key, f.get("default", "")) if "password" not in key else f.get("default", "")
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill="x", pady=int(5 * k))
            if kind == "check":
                v = tk.BooleanVar(value=bool(default))
                cb = tk.Checkbutton(row, text=f["label"], variable=v, bg=PANEL, fg=TEXT, activebackground=PANEL,
                                    selectcolor=PANEL, font=font(10), anchor="w", highlightthickness=0)
                cb.pack(fill="x")
                self.vars[key] = v
                continue
            if kind == "checks":     # несколько флажков (dict key → bool)
                tk.Label(row, text=f["label"], bg=PANEL, fg=MUTED, font=font(9, "bold"), anchor="w").pack(fill="x")
                grid = tk.Frame(row, bg=PANEL)
                grid.pack(fill="x")
                chosen = set(default or f.get("default", []))
                sub = {}
                for i, (ok, olabel) in enumerate(f["options"]):
                    bv = tk.BooleanVar(value=ok in chosen)
                    tk.Checkbutton(grid, text=olabel, variable=bv, bg=PANEL, fg=TEXT, activebackground=PANEL,
                                   selectcolor=PANEL, font=font(10), anchor="w", highlightthickness=0
                                   ).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, int(16 * k)))
                    sub[ok] = bv
                self.vars[key] = sub
                continue
            tk.Label(row, text=f["label"], bg=PANEL, fg=MUTED, font=font(9, "bold"), anchor="w").pack(fill="x")
            if kind == "choice":
                v = tk.StringVar(value=default if default in [o[0] for o in f["options"]] else f["options"][0][0])
                seg = tk.Frame(row, bg=PANEL)
                seg.pack(fill="x", pady=(int(3 * k), 0))
                btns = {}

                def pick(val, v=v, btns=btns):
                    v.set(val)
                    for kk, b in btns.items():
                        b.configure(bg=ACCENT if kk == val else CARD, fg="#FFFFFF" if kk == val else TEXT)
                for i, (val, lab) in enumerate(f["options"]):
                    b = tk.Label(seg, text=lab, bg=CARD, fg=TEXT, font=font(9), padx=int(9 * k), pady=int(5 * k),
                                 cursor="hand2")
                    b.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, int(4 * k)), pady=(0, int(4 * k)))
                    b.bind("<Button-1>", lambda e, val=val, pick=pick: pick(val))
                    btns[val] = b
                pick(v.get())
                self.vars[key] = v
            else:
                v = tk.StringVar(value=str(default))
                line = tk.Frame(row, bg=LINE, padx=1, pady=1)
                line.pack(fill="x", pady=(int(3 * k), 0))
                e = tk.Entry(line, textvariable=v, font=font(11), bd=0, bg="#FFFFFF", fg=TEXT,
                             insertbackground=TEXT, show="•" if kind == "password" else "", relief="flat")
                e.pack(fill="x", ipady=int(5 * k), padx=int(6 * k), side="left", expand=True)
                if kind == "file":
                    def browse(v=v, f=f):
                        p = filedialog.askopenfilename(parent=self.win, title=f["label"],
                                                       filetypes=f.get("filetypes", [("Все файлы", "*.*")]))
                        if p:
                            v.set(p)
                    FlatButton(line, "Обзор…", browse, size=9, padx=int(8 * k), pady=int(2 * k)).pack(side="right")
                first_entry = first_entry or e
                self.vars[key] = v
            if f.get("hint"):
                tk.Label(row, text=f["hint"], bg=PANEL, fg=MUTED, font=font(8), anchor="w", justify="left",
                         wraplength=int(400 * k)).pack(fill="x")
        bar = tk.Frame(box, bg=PANEL)
        bar.pack(fill="x", pady=(int(14 * k), 0))
        FlatButton(bar, ok_text, self.ok, kind="accent", size=10).pack(side="right")
        FlatButton(bar, "Отмена", self.cancel, size=10).pack(side="right", padx=(0, int(8 * k)))
        if extra_button:
            FlatButton(bar, extra_button[0], lambda: extra_button[1](self), size=9).pack(side="left")
        w.bind("<Return>", lambda e: self.ok())
        w.bind("<Escape>", lambda e: self.cancel())
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{(sw - w.winfo_reqwidth()) // 2}+{(sh - w.winfo_reqheight()) // 3}")
        w.attributes("-topmost", True)
        w.lift()
        w.focus_force()
        if first_entry:
            first_entry.focus_set()
        try:
            w.grab_set()
        except tk.TclError:
            pass

    def values(self) -> dict:
        out = {}
        for k, v in self.vars.items():
            out[k] = [kk for kk, bv in v.items() if bv.get()] if isinstance(v, dict) else v.get()
        return out

    def ok(self):
        vals = self.values()
        for f in self.fields:
            if f.get("required") and not str(vals.get(f["key"], "")).strip():
                messagebox.showwarning("Mandarin", f"Заполните поле «{f['label']}»", parent=self.win)
                return
        if hasattr(self, "validate"):
            err = self.validate(vals)
            if err:
                messagebox.showwarning("Mandarin", err, parent=self.win)
                return
        self.result = vals
        if self.memory_key:
            _remember(self.memory_key, vals)
        self.win.destroy()

    def cancel(self):
        self.result = None
        self.win.destroy()

    def run(self):
        self.win.wait_window()
        return self.result


def form(parent, title, subtitle, fields, ok_text="Готово", key=None, validate=None, extra_button=None):
    d = FormDialog(parent, title, subtitle, fields, ok_text, key, extra_button)
    if validate:
        d.validate = validate
    return d.run()


COLORS = [("red", "Красный"), ("gray", "Серый"), ("blue", "Синий"), ("orange", "Оранжевый")]
COLOR_RGB = {"red": (200, 30, 40), "gray": (110, 110, 120), "blue": (30, 90, 200), "orange": (255, 90, 10)}


# ───────────────────────── параметры функций ─────────────────────────

def ask(key: str, parent, files: list[Path]):
    """Параметры действия колеса/окна. None — отмена."""
    fn = ASKERS.get(key)
    return fn(parent, files) if fn else {}


def _protect(parent, files):
    def validate(v):
        if len(v["user_pw"]) < 3:
            return "Пароль слишком короткий (минимум 3 символа)"
        if v["user_pw"] != v["user_pw2"]:
            return "Пароли не совпадают"
    v = form(parent, "Защитить паролем", "PDF будет зашифрован AES-256. Без пароля файл не откроется.", [
        {"key": "user_pw", "label": "Пароль", "type": "password"},
        {"key": "user_pw2", "label": "Повторите пароль", "type": "password"},
        {"key": "allow_print", "label": "Разрешить печать", "type": "check", "default": True},
        {"key": "allow_copy", "label": "Разрешить копирование текста", "type": "check", "default": False},
        {"key": "allow_edit", "label": "Разрешить изменения и комментарии", "type": "check", "default": False},
    ], "Защитить", "protect", validate)
    if v:
        v.pop("user_pw2")
    return v


def _unlock(parent, files):
    return form(parent, "Снять пароль", "Нужен текущий пароль от файла.",
                [{"key": "password", "label": "Пароль", "type": "password", "required": True}], "Снять защиту")


def _watermark(parent, files):
    v = form(parent, "Водяной знак", "Полупрозрачная надпись по диагонали на каждой странице.", [
        {"key": "text", "label": "Текст", "default": "КОНФИДЕНЦИАЛЬНО", "required": True},
        {"key": "color", "label": "Цвет", "type": "choice", "options": COLORS, "default": "red"},
        {"key": "opacity", "label": "Прозрачность", "type": "choice", "default": "18",
         "options": [("10", "Еле видно"), ("18", "Обычно"), ("30", "Заметно"), ("50", "Ярко")]},
        {"key": "angle", "label": "Наклон", "type": "choice", "default": "45",
         "options": [("45", "По диагонали"), ("0", "Горизонтально"), ("90", "Вертикально")]},
        {"key": "pages", "label": "Страницы", "default": "", "hint": "Пусто — все. Например: 1-3,5"},
    ], "Добавить", "watermark")
    if v:
        v["color"] = COLOR_RGB[v["color"]]
        v["opacity"] = int(v["opacity"]) / 100
        v["angle"] = int(v["angle"])
    return v


def _numbers(parent, files):
    v = form(parent, "Номера страниц", "", [
        {"key": "fmt", "label": "Вид", "type": "choice", "default": "Стр. {n} из {total}",
         "options": [("{n}", "1"), ("Стр. {n}", "Стр. 1"), ("Стр. {n} из {total}", "Стр. 1 из 9"),
                     ("{n} / {total}", "1 / 9"), ("— {n} —", "— 1 —")]},
        {"key": "position", "label": "Где", "type": "choice", "default": "bottom-center",
         "options": list(pdftools.POSITIONS.items())},
        {"key": "start", "label": "Начать с номера", "default": "1"},
        {"key": "skip_first", "label": "Не нумеровать первую страницу (титул)", "type": "check"},
    ], "Пронумеровать", "numbers")
    if v:
        v["start"] = int(v["start"] or 1) if str(v["start"]).strip().isdigit() else 1
    return v


def _header_footer(parent, files):
    return form(parent, "Колонтитулы", "Подстановки: {file} — имя файла, {date} — дата, {n} — номер, "
                "{total} — всего страниц.", [
                    {"key": "header", "label": "Верхний колонтитул", "default": "{file}"},
                    {"key": "footer", "label": "Нижний колонтитул", "default": "{date} · стр. {n} из {total}"},
                ], "Добавить", "header_footer")


def _bates(parent, files):
    v = form(parent, "Нумерация Бейтса", "Сквозная нумерация всех страниц выбранных файлов — для юридических "
             f"документов. Файлов: {len(files)}.", [
                 {"key": "prefix", "label": "Префикс", "default": "ДЕЛО-"},
                 {"key": "start", "label": "Первый номер", "default": "1"},
                 {"key": "digits", "label": "Цифр в номере", "type": "choice", "default": "6",
                  "options": [("4", "0001"), ("5", "00001"), ("6", "000001"), ("8", "00000001")]},
                 {"key": "position", "label": "Где", "type": "choice", "default": "bottom-right",
                  "options": list(pdftools.POSITIONS.items())},
             ], "Пронумеровать", "bates")
    if v:
        v["start"] = int(v["start"]) if str(v["start"]).isdigit() else 1
        v["digits"] = int(v["digits"])
    return v


def _redact(parent, files):
    v = form(parent, "Зачернить данные", "Найденное закрывается чёрным и удаляется из файла навсегда — "
             "восстановить или скопировать будет нельзя.", [
                 {"key": "keys", "label": "Что искать", "type": "checks",
                  "default": ["email", "phone", "passport", "inn", "snils", "card"],
                  "options": [(k, lab) for k, (lab, _) in pdftools.REDACT_PATTERNS.items()]},
                 {"key": "words", "label": "Свои слова и фразы", "default": "",
                  "hint": "Через точку с запятой: Иванов; ООО «Ромашка»"},
                 {"key": "regex", "label": "Регулярное выражение (для опытных)", "default": ""},
             ], "Зачернить", "redact")
    if v:
        v["words"] = [w.strip() for w in v["words"].split(";") if w.strip()]
        v["regex"] = v["regex"].strip() or None
        if not v["keys"] and not v["words"] and not v["regex"]:
            return None
    return v


def _crop(parent, files):
    v = form(parent, "Обрезать поля", "", [
        {"key": "mode", "label": "Как", "type": "choice", "default": "auto",
         "options": [("auto", "По содержимому"), ("fixed", "Одинаково со всех сторон")]},
        {"key": "margin_mm", "label": "Отступ, мм", "default": "5",
         "hint": "По содержимому — сколько оставить вокруг текста; одинаково — сколько срезать."},
    ], "Обрезать", "crop")
    if v:
        try:
            v["margin_mm"] = float(str(v["margin_mm"]).replace(",", "."))
        except ValueError:
            v["margin_mm"] = 5.0
    return v


def _compare(parent, files):
    if len(files) >= 2:
        a, b = sorted(files[:2], key=lambda p: p.stat().st_mtime)
        return {"a": a, "b": b}
    p = filedialog.askopenfilename(parent=parent, title=f"С чем сравнить «{files[0].name}»?",
                                   initialdir=str(files[0].parent), filetypes=[("PDF", "*.pdf")])
    if not p:
        return None
    a, b = sorted([files[0], Path(p)], key=lambda x: x.stat().st_mtime)
    return {"a": a, "b": b}


def cert_dir() -> Path:
    d = store_dir() / "certs"
    d.mkdir(exist_ok=True)
    return d


def _new_cert(dlg):
    v = form(dlg.win, "Новый сертификат", "Самоподписанный сертификат — для внутренних документов. "
             "Для юридически значимой подписи используйте КЭП от удостоверяющего центра.", [
                 {"key": "name", "label": "ФИО или организация", "required": True},
                 {"key": "email", "label": "Эл. почта"},
                 {"key": "cert_password", "label": "Пароль сертификата", "type": "password", "required": True},
             ], "Создать")
    if v:
        safe = "".join(c for c in v["name"] if c.isalnum() or c in " -_").strip() or "сертификат"
        dst = cert_dir() / f"{safe}.pfx"
        pdftools.make_self_signed(dst, v["name"], v["email"], v["cert_password"])
        dlg.vars["pfx"].set(str(dst))
        dlg.vars["pfx_password"].set(v["cert_password"])
        messagebox.showinfo("Mandarin", f"Сертификат сохранён:\n{dst}", parent=dlg.win)


def _sign_cert(parent, files):
    existing = sorted(cert_dir().glob("*.pfx"))
    v = form(parent, "Подписать сертификатом", "Цифровая подпись PAdES: любое изменение файла после подписи "
             "будет обнаружено.", [
                 {"key": "pfx", "label": "Сертификат (.pfx / .p12)", "type": "file",
                  "default": str(existing[0]) if existing else "",
                  "filetypes": [("Сертификат", "*.pfx *.p12"), ("Все файлы", "*.*")]},
                 {"key": "pfx_password", "label": "Пароль сертификата", "type": "password"},
                 {"key": "reason", "label": "Причина", "default": "Согласовано"},
                 {"key": "location", "label": "Место", "default": ""},
                 {"key": "visible", "label": "Видимый штамп подписи на последней странице", "type": "check",
                  "default": True},
             ], "Подписать", "sign_cert", extra_button=("Создать сертификат…", _new_cert))
    if v:
        if not v["pfx"] or not Path(v["pfx"]).exists():
            messagebox.showwarning("Mandarin", "Выберите файл сертификата или создайте новый", parent=parent)
            return None
        v["pfx"] = Path(v["pfx"])
    return v


def _stamp(parent, files):
    v = form(parent, "Штамп", "", [
        {"key": "text", "label": "Штамп", "type": "choice", "default": "УТВЕРЖДЕНО",
         "options": [(s, s.capitalize()) for s in pdftools.STAMPS]},
        {"key": "custom", "label": "Или свой текст", "default": ""},
        {"key": "date", "label": "С датой", "type": "check", "default": True},
    ], "Выбрать", "stamp")
    if v and v["custom"].strip():
        v["text"] = v["custom"].strip().upper()
    return v


ASKERS = {
    "protect": _protect, "unlock": _unlock, "watermark": _watermark, "numbers": _numbers,
    "header_footer": _header_footer, "bates": _bates, "redact": _redact, "crop": _crop,
    "compare": _compare, "sign_cert": _sign_cert, "stamp": _stamp,
}


# ───────────────────────── подпись мышью ─────────────────────────

def signature_path() -> Path:
    return store_dir() / "signature.png"


class SignaturePad:
    """Рисуете подпись мышью → прозрачный PNG. Сохраняется для следующих документов."""

    def __init__(self, parent):
        self.k = k = ui_scale(parent)
        self.result: bytes | None = None
        self.strokes: list[list[tuple[float, float]]] = []
        w = self.win = tk.Toplevel(parent)
        w.title("Подпись")
        w.configure(bg=PANEL)
        w.resizable(False, False)
        box = tk.Frame(w, bg=PANEL, padx=int(20 * k), pady=int(16 * k))
        box.pack()
        tk.Label(box, text="Распишитесь мышью или пером", bg=PANEL, fg=TEXT, font=font(13, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(box, text="Подпись запомнится для следующих документов.", bg=PANEL, fg=MUTED, font=font(9),
                 anchor="w").pack(fill="x", pady=(0, int(8 * k)))
        self.W, self.H = int(520 * k), int(200 * k)
        self.c = tk.Canvas(box, width=self.W, height=self.H, bg="#FFFFFF", highlightthickness=1,
                           highlightbackground=LINE, cursor="pencil")
        self.c.pack()
        self.c.create_line(int(30 * k), self.H - int(45 * k), self.W - int(30 * k), self.H - int(45 * k),
                           fill="#D8D8DE", dash=(4, 3), tags="guide")
        self.c.bind("<ButtonPress-1>", self._start)
        self.c.bind("<B1-Motion>", self._move)
        bar = tk.Frame(box, bg=PANEL)
        bar.pack(fill="x", pady=(int(12 * k), 0))
        FlatButton(bar, "Использовать", self.ok, kind="accent").pack(side="right")
        FlatButton(bar, "Отмена", self.cancel).pack(side="right", padx=(0, int(8 * k)))
        FlatButton(bar, "Очистить", self.clear).pack(side="left")
        FlatButton(bar, "Из картинки…", self.from_file).pack(side="left", padx=(int(8 * k), 0))
        w.bind("<Escape>", lambda e: self.cancel())
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        w.update_idletasks()
        w.geometry(f"+{(w.winfo_screenwidth() - w.winfo_reqwidth()) // 2}+{(w.winfo_screenheight() - w.winfo_reqheight()) // 3}")
        w.attributes("-topmost", True)
        w.focus_force()
        try:
            w.grab_set()
        except tk.TclError:
            pass

    def _start(self, e):
        self.strokes.append([(e.x, e.y)])

    def _move(self, e):
        if not self.strokes:
            return
        x0, y0 = self.strokes[-1][-1]
        self.strokes[-1].append((e.x, e.y))
        self.c.create_line(x0, y0, e.x, e.y, fill="#1A2A6C", width=max(2, int(2.6 * self.k)),
                           capstyle="round", smooth=True, tags="ink")

    def clear(self):
        self.strokes.clear()
        self.c.delete("ink")

    def from_file(self):
        p = filedialog.askopenfilename(parent=self.win, title="Картинка подписи",
                                       filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if p:
            self.result = signature_from_image(Path(p))
            signature_path().write_bytes(self.result)
            self.win.destroy()

    def ok(self):
        if not self.strokes:
            messagebox.showinfo("Mandarin", "Сначала распишитесь", parent=self.win)
            return
        self.result = strokes_to_png(self.strokes)
        signature_path().write_bytes(self.result)
        self.win.destroy()

    def cancel(self):
        self.result = None
        self.win.destroy()

    def run(self):
        self.win.wait_window()
        return self.result


def strokes_to_png(strokes, color=(26, 42, 108)) -> bytes:
    """Штрихи → сглаженный прозрачный PNG, обрезанный по подписи."""
    from PIL import Image, ImageDraw
    S = 4
    xs = [x for s in strokes for x, _ in s]
    ys = [y for s in strokes for _, y in s]
    pad = 8
    x0, y0 = min(xs) - pad, min(ys) - pad
    W, H = int((max(xs) - x0 + pad) * S), int((max(ys) - y0 + pad) * S)
    im = Image.new("RGBA", (max(W, 4), max(H, 4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for s in strokes:
        pts = [((x - x0) * S, (y - y0) * S) for x, y in s]
        if len(pts) == 1:
            x, y = pts[0]
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color + (255,))
        else:
            d.line(pts, fill=color + (255,), width=int(2.6 * S), joint="curve")
            for x, y in (pts[0], pts[-1]):
                d.ellipse((x - 1.3 * S, y - 1.3 * S, x + 1.3 * S, y + 1.3 * S), fill=color + (255,))
    im = im.resize((max(1, W // 2), max(1, H // 2)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def signature_from_image(p: Path) -> bytes:
    """Скан подписи на белом фоне → прозрачный PNG (белое убирается)."""
    from PIL import Image, ImageOps
    im = Image.open(p).convert("L")
    im = ImageOps.autocontrast(im)
    alpha = im.point(lambda v: 255 if v < 140 else (0 if v > 220 else int((220 - v) * 255 / 80)))
    rgba = Image.new("RGBA", im.size, (26, 42, 108, 0))
    rgba.putalpha(alpha)
    bbox = alpha.getbbox()
    if bbox:
        rgba = rgba.crop(bbox)
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return buf.getvalue()


def get_signature(parent, force_new=False) -> bytes | None:
    p = signature_path()
    if p.exists() and not force_new:
        return p.read_bytes()
    return SignaturePad(parent).run()
