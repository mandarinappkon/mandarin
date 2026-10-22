"""Общие цвета, шрифты и плоские кнопки для окон Mandarin (Tkinter)."""
from __future__ import annotations

import os
import sys
import tkinter as tk

IS_WIN = os.name == "nt"
FONT = "Segoe UI" if IS_WIN else "DejaVu Sans"

BG = "#F4F4F5"          # фон окна (светлый, как у Tangerine)
PANEL = "#FFFFFF"       # панели
CARD = "#EEEEF0"        # карточки и кнопки
CARD_HOVER = "#E3E3E7"
LINE = "#E0E0E5"
TEXT = "#141418"
MUTED = "#6F6F7A"
ACCENT = "#FF5A0A"      # мандариновый, как выделенная плитка в колесе
ACCENT_HOVER = "#FF7A33"
ACCENT_TEXT = "#FFFFFF"
GREEN = "#1F8A5B"
DANGER = "#D93A40"

FILE_COLORS = ["#FF5A0A", "#4C8DFF", "#2E9E6A", "#C05BFF", "#FF4D6D", "#FFC233", "#22B8CF", "#A3E635"]


def ui_scale(widget) -> float:
    try:
        return max(1.0, widget.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def font(size: int, weight: str = "normal"):
    return (FONT, size, weight) if weight != "normal" else (FONT, size)


class FlatButton(tk.Label):
    """Кнопка без системной рамки: работает одинаково в Windows и Linux."""

    def __init__(self, master, text, command=None, kind="normal", size=10, padx=12, pady=7, **kw):
        palettes = {
            "normal": (CARD, CARD_HOVER, TEXT),
            "accent": (ACCENT, ACCENT_HOVER, ACCENT_TEXT),
            "ghost": (PANEL, CARD, TEXT),
            "danger": (CARD, "#FBE3E4", "#C9353B"),
        }
        self._bg, self._hover, fg = palettes[kind]
        self._fg = fg
        super().__init__(master, text=text, bg=self._bg, fg=fg, font=font(size, "bold" if kind == "accent" else "normal"),
                         padx=padx, pady=pady, cursor="hand2", **kw)
        self._command = command
        self._enabled = True
        self.bind("<Enter>", lambda e: self._enabled and self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<Button-1>", self._click)

    def _click(self, _e):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, on: bool):
        self._enabled = on
        self.configure(fg=self._fg if on else MUTED, cursor="hand2" if on else "arrow")


def tooltip(widget, text: str):
    tip = {"w": None}

    def show(_e):
        if tip["w"]:
            return
        t = tk.Toplevel(widget)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=text, bg="#2A2A31", fg="#FFFFFF", font=font(9), padx=8, pady=4).pack()
        t.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
        tip["w"] = t

    def hide(_e):
        if tip["w"]:
            tip["w"].destroy()
            tip["w"] = None
    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<Button-1>", hide, add="+")


def shortcut_key(event) -> str:
    """Буква клавиши независимо от раскладки (Ctrl+Z работает и на русской)."""
    if IS_WIN and 65 <= event.keycode <= 90:
        return chr(event.keycode).lower()
    ks = event.keysym
    ru = {"Cyrillic_ya": "z", "Cyrillic_ef": "a", "Cyrillic_yeru": "s", "Cyrillic_ve": "d",
          "Cyrillic_ka": "r", "Cyrillic_er": "h", "Cyrillic_es": "c"}
    return ru.get(ks, ks.lower() if len(ks) == 1 else ks)


def dnd_available(widget) -> bool:
    try:
        widget.tk.call("package", "present", "tkdnd")
        return True
    except tk.TclError:
        return False


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
