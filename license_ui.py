"""Окно лицензии Mandarin: статус, ввод ключа, покупка."""
from __future__ import annotations

import tkinter as tk
import webbrowser

import license as L
from ui_kit import ACCENT, DANGER, GREEN, LINE, MUTED, PANEL, TEXT, FlatButton, font, ui_scale


class LicenseDialog:
    def __init__(self, parent, reason: str | None = None, on_change=None):
        self.k = k = ui_scale(parent)
        self.on_change = on_change
        self.activated = False
        w = self.win = tk.Toplevel(parent)
        w.title("Mandarin Pro")
        w.configure(bg=PANEL)
        w.resizable(False, False)
        box = tk.Frame(w, bg=PANEL, padx=int(24 * k), pady=int(20 * k))
        box.pack(fill="both", expand=True)

        tk.Label(box, text="Mandarin Pro", bg=PANEL, fg=TEXT, font=font(16, "bold"), anchor="w").pack(fill="x")
        if reason:
            tk.Label(box, text=reason, bg=PANEL, fg=ACCENT, font=font(10), anchor="w", justify="left",
                     wraplength=int(420 * k)).pack(fill="x", pady=(int(4 * k), 0))
        tk.Label(box, text="PDF-редактор, подпись, OCR, пароли, зачернение, водяной знак, сравнение версий, "
                 "сертификаты, PDF/A, экспорт в Excel и PowerPoint. Конвертер файлов остаётся бесплатным.",
                 bg=PANEL, fg=MUTED, font=font(9), anchor="w", justify="left",
                 wraplength=int(420 * k)).pack(fill="x", pady=(int(6 * k), int(12 * k)))

        self.status = tk.Label(box, bg=PANEL, font=font(10, "bold"), anchor="w", justify="left",
                               wraplength=int(420 * k))
        self.status.pack(fill="x")

        tk.Label(box, text="КЛЮЧ ИЗ ПИСЬМА", bg=PANEL, fg=MUTED, font=font(8, "bold"), anchor="w"
                 ).pack(fill="x", pady=(int(14 * k), int(3 * k)))
        line = tk.Frame(box, bg=LINE, padx=1, pady=1)
        line.pack(fill="x")
        self.text = tk.Text(line, height=3, width=52, font=("Consolas", 10), bd=0, bg="#FFFFFF", fg=TEXT,
                            wrap="char", relief="flat", padx=int(6 * k), pady=int(5 * k))
        self.text.pack(fill="x")
        self.msg = tk.Label(box, text="", bg=PANEL, fg=DANGER, font=font(9), anchor="w", justify="left",
                            wraplength=int(420 * k))
        self.msg.pack(fill="x", pady=(int(4 * k), 0))

        bar = tk.Frame(box, bg=PANEL)
        bar.pack(fill="x", pady=(int(12 * k), 0))
        FlatButton(bar, "Активировать", self.activate, kind="accent").pack(side="right")
        FlatButton(bar, "Купить Pro", lambda: webbrowser.open(L.BUY_URL)).pack(side="right", padx=(0, int(8 * k)))
        self.deact = FlatButton(bar, "Отвязать ключ", self.deactivate, size=9)
        tk.Label(box, text=f"Потеряли ключ? Напишите на {L.SUPPORT_EMAIL} — пришлём повторно.", bg=PANEL,
                 fg=MUTED, font=font(8), anchor="w").pack(fill="x", pady=(int(12 * k), 0))

        w.bind("<Escape>", lambda e: w.destroy())
        self.text.bind("<Control-Return>", lambda e: (self.activate(), "break")[1])
        self.refresh()
        w.update_idletasks()
        w.geometry(f"+{(w.winfo_screenwidth() - w.winfo_reqwidth()) // 2}+{(w.winfo_screenheight() - w.winfo_reqheight()) // 3}")
        w.attributes("-topmost", True)
        w.lift()
        w.focus_force()
        self.text.focus_set()
        try:
            w.grab_set()
        except tk.TclError:
            pass

    def refresh(self):
        lic = L.current()
        if lic:
            self.status.configure(text=f"✓ Активировано: {lic.edition_name}, лицензия от "
                                  f"{lic.issued.strftime('%d.%m.%Y')}", fg=GREEN)
            self.deact.pack(side="left")
        else:
            left = L.trial_days_left()
            self.status.configure(text=(f"Пробный период: осталось {left} дн. — все функции открыты" if left else
                                        "Пробный период закончился — Pro-функции заблокированы"),
                                  fg=TEXT if left else DANGER)
            self.deact.pack_forget()

    def activate(self):
        key = self.text.get("1.0", "end").strip()
        if not key:
            self.msg.configure(text="Вставьте ключ из письма (Ctrl+V)")
            return
        try:
            lic = L.activate(key)
        except L.LicenseError as e:
            self.msg.configure(text=str(e))
            return
        self.activated = True
        self.msg.configure(text="")
        self.text.delete("1.0", "end")
        self.refresh()
        if self.on_change:
            self.on_change(lic)
        self.win.after(1200, self.win.destroy)

    def deactivate(self):
        L.deactivate()
        self.refresh()
        if self.on_change:
            self.on_change(None)

    def run(self) -> bool:
        self.win.wait_window()
        return self.activated


def require_pro(parent, action=None, on_change=None) -> bool:
    """True — можно выполнять. Иначе показывает окно покупки/активации."""
    if action is None or not L.needs_pro(action):
        return True
    name = getattr(action, "hint", None) or "Эта функция"
    return LicenseDialog(parent, f"«{name}» входит в Mandarin Pro. Пробный период закончился.",
                         on_change).run()
