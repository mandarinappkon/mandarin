"""
Отрисовка колеса в стиле Tangerine (Pillow, со сглаживанием).

Светлая подложка, отдельные скруглённые плитки с зазорами, первая плитка — сверху по центру.
Выбранная плитка заливается мандариновым цветом с белой подписью. В центре — белая
«таблетка» с выбранным форматом (или размером файла). У инструментов — линейная иконка
над подписью капслоком.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps

SS = 3                                  # суперсэмплинг для сглаживания

# Цвета (KEY — цвет-ключ прозрачности окна; близок к кромке колеса, чтобы не было ореола)
KEY = (207, 209, 212)
RIM = (209, 209, 212)                   # кромка подложки
DISC = (231, 231, 234)                  # подложка
TILE = (245, 245, 246)                  # плитка
TILE_EDGE = (222, 222, 226)             # тонкая кромка плитки
HOLE_RING = (236, 236, 239)
PILL = (255, 255, 255)
PILL_EDGE = (226, 226, 230)
INK = (17, 17, 20)                      # подписи
INK_SOFT = (60, 60, 68)
ORANGE = (255, 90, 10)                  # выбранная плитка
ORANGE_EDGE = (238, 76, 0)
WHITE = (255, 255, 255)
CAPTION_INK = (95, 95, 105)


def _font_paths(bold: bool):
    win = os.environ.get("WINDIR", r"C:\Windows")
    names = (["segoeuib.ttf", "seguisb.ttf"] if bold else ["segoeui.ttf"])
    cands = [os.path.join(win, "Fonts", n) for n in names]
    cands += ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    return cands


@lru_cache(maxsize=64)
def font(px: int, bold: bool = True):
    for path in _font_paths(bold):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)
    except TypeError:
        return ImageFont.load_default()


class Geometry:
    """Размеры колеса в экранных пикселях (умножаются на масштаб DPI)."""

    def __init__(self, scale: float = 1.0, caption: bool = True):
        k = scale
        self.k = k
        self.R_DISC = round(132 * k)        # подложка
        self.R_OUT = round(125 * k)         # внешний край плиток
        self.R_IN = round(55 * k)           # внутренний край плиток
        self.R_HOLE = round(51 * k)         # центральный круг
        self.GAP = 5.5 * k                  # зазор между плитками
        self.CORNER = 11 * k                # скругление углов плиток
        self.PAD = round(4 * k)
        self.CAPTION_H = round(34 * k) if caption else 0
        self.W = 2 * (self.R_DISC + self.PAD)
        self.H = self.W + self.CAPTION_H
        self.CX = self.CY = self.R_DISC + self.PAD

    def index_at(self, x: float, y: float, n: int) -> int:
        """Номер плитки под точкой (координаты внутри окна) или -1 (центр/снаружи)."""
        dx, dy = x - self.CX, self.CY - y
        dist = math.hypot(dx, dy)
        if n == 0 or dist < self.R_IN - 2 * self.k or dist > self.R_DISC + self.PAD:
            return -1
        step = 360 / n
        ang = math.degrees(math.atan2(dy, dx))          # 0° — вправо, против часовой
        return int(((90 - ang + step / 2) % 360) // step)

    def in_center(self, x, y) -> bool:
        return math.hypot(x - self.CX, y - self.CY) < self.R_IN - 2 * self.k


def _tile_shape(draw, cx, cy, r_in, r_out, a0, a1, gap, corner, fill, steps=28):
    """Скруглённая плитка-сектор: сжатый сектор + обводка радиусом corner (сумма Минковского)."""
    d = gap / 2 + corner
    ri, ro = r_in + corner, r_out - corner
    if ro <= ri:
        return

    def side_angle(r, base, sign):
        return base + sign * math.degrees(math.asin(min(0.999, d / r)))

    # плитка рисуется по часовой от a0 до a1 (углы в «экранных» градусах: 0 — вверх)
    pts = []
    s0, s1 = side_angle(ro, a0, 1), side_angle(ro, a1, -1)
    for i in range(steps + 1):
        a = s0 + (s1 - s0) * i / steps
        pts.append(_polar(cx, cy, ro, a))
    s0i, s1i = side_angle(ri, a0, 1), side_angle(ri, a1, -1)
    if s1i <= s0i:                       # внутренний край выродился (очень много плиток)
        mid = (a0 + a1) / 2
        s0i = s1i = mid
    for i in range(steps + 1):
        a = s1i + (s0i - s1i) * i / steps
        pts.append(_polar(cx, cy, ri, a))
    draw.polygon(pts, fill=fill)
    # сумма Минковского с кругом: круги вдоль всего контура (без «усиков» толстых линий PIL)
    ring = pts + [pts[0]]
    step_px = max(1.0, corner / 3)
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step_px))
        for j in range(n):
            x, y = x0 + (x1 - x0) * j / n, y0 + (y1 - y0) * j / n
            draw.ellipse((x - corner, y - corner, x + corner, y + corner), fill=fill)


def _polar(cx, cy, r, a_deg_from_top_cw):
    a = math.radians(a_deg_from_top_cw)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def _text_center(draw, xy, text, fnt, fill):
    draw.text(xy, text, font=fnt, fill=fill, anchor="mm")


def _fit_font(draw, text, max_w, px, bold=True, min_px=7):
    while px > min_px:
        f = font(px, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        px -= 1
    return font(min_px, bold)


# ───────────────────────── иконки инструментов (линейные, как у Tangerine) ─────────────────────────

def draw_icon(d: ImageDraw.ImageDraw, name: str, cx: float, cy: float, s: float, col, w: float):
    """s — половина размера иконки, w — толщина линии (всё в пикселях холста)."""
    w = max(1, int(round(w)))
    L = lambda *p: d.line(p, fill=col, width=w, joint="curve")

    def arrow_head(x, y, ang, size):
        a = math.radians(ang)
        for da in (150, -150):
            b = a + math.radians(da)
            L((x, y), (x + size * math.cos(b), y + size * math.sin(b)))

    if name == "compress":            # две стрелки внутрь
        for sx, sy in ((-1, -1), (1, 1)):
            x0, y0 = cx + sx * s, cy + sy * s
            x1, y1 = cx + sx * s * 0.25, cy + sy * s * 0.25
            L((x0, y0), (x1, y1))
            L((x1, y1), (x1 + sx * s * 0.45, y1))
            L((x1, y1), (x1, y1 + sy * s * 0.45))
    elif name == "rotate":            # дуга со стрелкой
        d.arc((cx - s, cy - s, cx + s, cy + s), start=-60, end=230, fill=col, width=w)
        x, y = cx + s * math.cos(math.radians(-60)), cy + s * math.sin(math.radians(-60))
        arrow_head(x, y, 30, s * 0.5)
    elif name == "half":              # большой и маленький прямоугольник
        d.rectangle((cx - s, cy - s * 0.8, cx + s * 0.2, cy + s * 0.8), outline=col, width=w)
        d.rectangle((cx + s * 0.4, cy, cx + s, cy + s * 0.8), outline=col, width=w)
    elif name == "noexif":            # бирка, перечёркнутая
        d.polygon([(cx - s * 0.9, cy - s * 0.3), (cx - s * 0.3, cy - s * 0.9), (cx + s * 0.9, cy - s * 0.9),
                   (cx + s * 0.9, cy + s * 0.3), (cx + s * 0.3, cy + s * 0.9)], outline=col, width=w)
        d.ellipse((cx + s * 0.25, cy - s * 0.55, cx + s * 0.55, cy - s * 0.25), outline=col, width=w)
        L((cx - s, cy + s), (cx + s, cy - s))
    elif name == "qr":
        q = s * 0.8
        for ox, oy in ((-1, -1), (1, -1), (-1, 1)):
            x, y = cx + ox * s * 0.55, cy + oy * s * 0.55
            d.rectangle((x - q * 0.45, y - q * 0.45, x + q * 0.45, y + q * 0.45), outline=col, width=w)
        d.rectangle((cx + s * 0.35, cy + s * 0.35, cx + s * 0.75, cy + s * 0.75), fill=col)
    elif name == "zip":               # коробка с «молнией»
        d.rounded_rectangle((cx - s * 0.8, cy - s, cx + s * 0.8, cy + s), radius=s * 0.2, outline=col, width=w)
        for i in range(4):
            y = cy - s * 0.75 + i * s * 0.32
            L((cx - s * 0.18 if i % 2 else cx, y), (cx + (0 if i % 2 else s * 0.18), y))
    elif name == "hash":
        for o in (-0.35, 0.35):
            L((cx + s * o + s * 0.15, cy - s), (cx + s * o - s * 0.15, cy + s))
            L((cx - s, cy + s * o), (cx + s, cy + s * o))
    elif name == "path":              # звено цепи
        for o in (-1, 1):
            x = cx + o * s * 0.38
            d.rounded_rectangle((x - s * 0.62, cy - s * 0.38, x + s * 0.62, cy + s * 0.38),
                                radius=s * 0.38, outline=col, width=w)
    elif name == "split":             # ножницы
        for o in (-1, 1):
            d.ellipse((cx - s, cy + o * s * 0.6 - s * 0.32, cx - s * 0.36, cy + o * s * 0.6 + s * 0.32),
                      outline=col, width=w)
            L((cx - s * 0.4, cy + o * s * 0.4), (cx + s, cy - o * s * 0.6))
    elif name == "merge":             # две страницы
        d.rectangle((cx - s, cy - s, cx + s * 0.4, cy + s * 0.5), outline=col, width=w)
        d.rectangle((cx - s * 0.4, cy - s * 0.5, cx + s, cy + s), outline=col, width=w)
    elif name == "organize":          # сетка 2×2
        for ox in (-1, 1):
            for oy in (-1, 1):
                x, y = cx + ox * s * 0.5, cy + oy * s * 0.5
                d.rounded_rectangle((x - s * 0.38, y - s * 0.38, x + s * 0.38, y + s * 0.38),
                                    radius=s * 0.1, outline=col, width=w)
    elif name == "images":            # картинка с горами
        d.rounded_rectangle((cx - s, cy - s * 0.8, cx + s, cy + s * 0.8), radius=s * 0.15, outline=col, width=w)
        L((cx - s * 0.8, cy + s * 0.6), (cx - s * 0.2, cy), (cx + s * 0.2, cy + s * 0.35),
          (cx + s * 0.45, cy + s * 0.1), (cx + s * 0.8, cy + s * 0.6))
        d.ellipse((cx + s * 0.25, cy - s * 0.55, cx + s * 0.55, cy - s * 0.25), fill=col)
    elif name == "mute":              # динамик с крестиком
        d.polygon([(cx - s, cy - s * 0.35), (cx - s * 0.5, cy - s * 0.35), (cx - s * 0.05, cy - s * 0.8),
                   (cx - s * 0.05, cy + s * 0.8), (cx - s * 0.5, cy + s * 0.35), (cx - s, cy + s * 0.35)],
                  outline=col, width=w)
        L((cx + s * 0.3, cy - s * 0.35), (cx + s, cy + s * 0.35))
        L((cx + s * 0.3, cy + s * 0.35), (cx + s, cy - s * 0.35))
    elif name == "loud":              # эквалайзер
        for i, h in enumerate((0.5, 1.0, 0.7, 0.35)):
            x = cx - s * 0.75 + i * s * 0.5
            L((x, cy + s * 0.8), (x, cy + s * 0.8 - 1.6 * s * h))
    elif name == "mono":              # одна волна
        d.ellipse((cx - s * 0.75, cy - s * 0.75, cx + s * 0.75, cy + s * 0.75), outline=col, width=w)
        d.ellipse((cx - s * 0.2, cy - s * 0.2, cx + s * 0.2, cy + s * 0.2), fill=col)
    elif name == "frame":             # кадр плёнки
        d.rounded_rectangle((cx - s, cy - s * 0.75, cx + s, cy + s * 0.75), radius=s * 0.12, outline=col, width=w)
        for o in (-1, 1):
            x = cx + o * s * 0.62
            L((x, cy - s * 0.75), (x, cy + s * 0.75))
    elif name == "text":
        L((cx - s * 0.8, cy - s * 0.8), (cx + s * 0.8, cy - s * 0.8))
        L((cx, cy - s * 0.8), (cx, cy + s * 0.85))
    elif name == "doc":               # документ с загнутым углом
        d.polygon([(cx - s * 0.7, cy - s), (cx + s * 0.25, cy - s), (cx + s * 0.7, cy - s * 0.55),
                   (cx + s * 0.7, cy + s), (cx - s * 0.7, cy + s)], outline=col, width=w)
        L((cx + s * 0.25, cy - s), (cx + s * 0.25, cy - s * 0.55), (cx + s * 0.7, cy - s * 0.55))
        for yy in (-0.1, 0.25, 0.6):
            L((cx - s * 0.4, cy + s * yy), (cx + s * 0.4, cy + s * yy))
    elif name == "pen":               # перо и росчерк
        L((cx - s * 0.2, cy + s * 0.3), (cx + s * 0.75, cy - s * 0.65))
        L((cx - s * 0.2, cy + s * 0.3), (cx - s * 0.35, cy + s * 0.5), (cx + s * 0.95, cy - s * 0.45))
        L((cx - s, cy + s * 0.85), (cx - s * 0.6, cy + s * 0.55), (cx - s * 0.2, cy + s * 0.9), (cx + s * 0.3, cy + s * 0.65))
    elif name in ("lock", "unlock"):  # замок
        d.rounded_rectangle((cx - s * 0.8, cy - s * 0.1, cx + s * 0.8, cy + s), radius=s * 0.15, outline=col, width=w)
        if name == "lock":
            d.arc((cx - s * 0.5, cy - s * 0.95, cx + s * 0.5, cy + s * 0.15), start=180, end=360, fill=col, width=w)
            L((cx - s * 0.5, cy - s * 0.4), (cx - s * 0.5, cy - s * 0.1))
            L((cx + s * 0.5, cy - s * 0.4), (cx + s * 0.5, cy - s * 0.1))
        else:
            d.arc((cx - s * 0.1, cy - s * 1.05, cx + s * 0.9, cy + s * 0.05), start=180, end=360, fill=col, width=w)
            L((cx - s * 0.1, cy - s * 0.5), (cx - s * 0.1, cy - s * 0.25))
        d.ellipse((cx - s * 0.12, cy + s * 0.3, cx + s * 0.12, cy + s * 0.54), fill=col)
    elif name == "scan":              # рамка распознавания с буквой
        for sx in (-1, 1):
            for sy in (-1, 1):
                x, y = cx + sx * s, cy + sy * s
                L((x, y - sy * s * 0.45), (x, y), (x - sx * s * 0.45, y))
        L((cx - s * 0.35, cy + s * 0.45), (cx, cy - s * 0.45), (cx + s * 0.35, cy + s * 0.45))
        L((cx - s * 0.2, cy + s * 0.12), (cx + s * 0.2, cy + s * 0.12))
    elif name == "redact":            # строки, одна закрыта
        L((cx - s, cy - s * 0.6), (cx + s, cy - s * 0.6))
        d.rectangle((cx - s, cy - s * 0.2, cx + s * 0.6, cy + s * 0.25), fill=col)
        L((cx - s, cy + s * 0.65), (cx + s * 0.5, cy + s * 0.65))
    elif name == "drop":              # капля = водяной знак
        d.polygon([(cx, cy - s), (cx + s * 0.62, cy + s * 0.15), (cx + s * 0.45, cy + s * 0.75), (cx, cy + s),
                   (cx - s * 0.45, cy + s * 0.75), (cx - s * 0.62, cy + s * 0.15)], outline=col, width=w)
    elif name == "numbers":           # «1 2 3» на странице
        d.rectangle((cx - s * 0.75, cy - s, cx + s * 0.75, cy + s), outline=col, width=w)
        d.text((cx, cy + s * 0.5), "12", font=font(int(s * 0.9), True), fill=col, anchor="mm")
    elif name == "header":            # страница с полосками сверху и снизу
        d.rectangle((cx - s * 0.75, cy - s, cx + s * 0.75, cy + s), outline=col, width=w)
        d.rectangle((cx - s * 0.5, cy - s * 0.75, cx + s * 0.5, cy - s * 0.5), fill=col)
        d.rectangle((cx - s * 0.5, cy + s * 0.5, cx + s * 0.5, cy + s * 0.75), fill=col)
    elif name == "crop":              # уголки обрезки
        L((cx - s * 0.55, cy - s), (cx - s * 0.55, cy + s * 0.55), (cx + s, cy + s * 0.55))
        L((cx - s, cy - s * 0.55), (cx + s * 0.55, cy - s * 0.55), (cx + s * 0.55, cy + s))
    elif name == "compare":           # две страницы и стрелки
        d.rectangle((cx - s, cy - s * 0.8, cx - s * 0.15, cy + s * 0.8), outline=col, width=w)
        d.rectangle((cx + s * 0.15, cy - s * 0.8, cx + s, cy + s * 0.8), outline=col, width=w)
        L((cx - s * 0.75, cy - s * 0.2), (cx - s * 0.4, cy - s * 0.2))
        L((cx + s * 0.4, cy + s * 0.2), (cx + s * 0.75, cy + s * 0.2))
    elif name == "cert":              # медаль с лентами
        d.ellipse((cx - s * 0.6, cy - s, cx + s * 0.6, cy + s * 0.2), outline=col, width=w)
        L((cx - s * 0.35, cy + s * 0.05), (cx - s * 0.55, cy + s), (cx - s * 0.1, cy + s * 0.7))
        L((cx + s * 0.35, cy + s * 0.05), (cx + s * 0.55, cy + s), (cx + s * 0.1, cy + s * 0.7))
    elif name == "shield":            # щит с галочкой
        d.polygon([(cx, cy - s), (cx + s * 0.85, cy - s * 0.65), (cx + s * 0.7, cy + s * 0.35), (cx, cy + s),
                   (cx - s * 0.7, cy + s * 0.35), (cx - s * 0.85, cy - s * 0.65)], outline=col, width=w)
        L((cx - s * 0.35, cy), (cx - s * 0.05, cy + s * 0.3), (cx + s * 0.4, cy - s * 0.3))
    elif name == "layers":            # слои = «сгладить»
        for o in (-0.45, 0.05, 0.55):
            d.polygon([(cx, cy + s * (o - 0.45)), (cx + s, cy + s * o), (cx, cy + s * (o + 0.45)), (cx - s, cy + s * o)],
                      outline=col, width=w)
    elif name == "more":              # три точки
        for o in (-0.6, 0, 0.6):
            d.ellipse((cx + s * o - s * 0.17, cy - s * 0.17, cx + s * o + s * 0.17, cy + s * 0.17), fill=col)
    else:                             # точка по умолчанию
        d.ellipse((cx - s * 0.3, cy - s * 0.3, cx + s * 0.3, cy + s * 0.3), fill=col)


ICON_FOR = {
    "organize": "organize", "img_compress": "compress", "aud_compress": "compress", "vid_compress": "compress",
    "pdf_compress": "compress", "img_half": "half", "img_rotate": "rotate", "pdf_rotate": "rotate",
    "img_strip": "noexif", "pdf_strip": "noexif", "img_qr": "qr", "any_zip": "zip", "any_sha": "hash",
    "any_path": "path", "pdf_split": "split", "pdf_merge": "merge", "mix_pdf": "merge", "pdf_imgs": "images",
    "vid_mute": "mute", "aud_norm": "loud", "aud_mono": "mono", "vid_frame": "frame",
    "pdf_open": "doc", "pdf_sign": "pen", "pdf_protect": "lock", "pdf_unlock": "unlock", "pdf_ocr": "scan",
    "pdf_redact": "redact", "pdf_watermark": "drop", "pdf_numbers": "numbers", "pdf_headers": "header",
    "pdf_bates": "hash", "pdf_crop": "crop", "pdf_compare": "compare", "pdf_cert": "cert", "pdf_verify": "shield",
    "pdf_flatten": "layers", "_more": "more",
}


# ───────────────────────── колесо ─────────────────────────

def render_wheel(labels: list[str], hover: int, center_text: str, caption: str = "",
                 icons: list[str | None] | None = None, scale: float = 1.0) -> Image.Image:
    """Возвращает RGB-картинку колеса на фоне KEY (для окна с цветом-ключом прозрачности)."""
    g = Geometry(scale)
    S = SS
    W, H = g.W * S, g.H * S
    im = Image.new("RGB", (W, H), KEY)
    d = ImageDraw.Draw(im)
    cx, cy = g.CX * S, g.CY * S

    # подложка: кромка + диск
    R = g.R_DISC * S
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=RIM)
    r2 = R - 2 * S * g.k
    d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), fill=DISC)

    n = len(labels)
    if n:
        step = 360 / n
        for i, label in enumerate(labels):
            a0 = i * step - step / 2
            a1 = a0 + step
            sel = i == hover
            edge = ORANGE_EDGE if sel else TILE_EDGE
            fill = ORANGE if sel else TILE
            # кромка (чуть больше) + сама плитка — даёт мягкий «объём»
            _tile_shape(d, cx, cy, g.R_IN * S - S, g.R_OUT * S + S, a0, a1, g.GAP * S - 2 * S,
                        g.CORNER * S + S, edge)
            _tile_shape(d, cx, cy, g.R_IN * S, g.R_OUT * S, a0, a1, g.GAP * S, g.CORNER * S, fill)
            # подпись
            mid = a0 + step / 2
            rr = (g.R_IN + g.R_OUT) / 2 * S + 2 * S * g.k
            tx, ty = _polar(cx, cy, rr, mid)
            chord = 2 * rr * math.sin(math.radians(step / 2)) - 2 * g.GAP * S - 6 * S * g.k
            max_w = min(chord, (g.R_OUT - g.R_IN) * S * 1.05)
            ink = WHITE if sel else INK
            icon = icons[i] if icons else None
            if icon:
                txt = label.upper()
                f = _fit_font(d, txt, max_w, int(10 * g.k * S))
                s = 7.5 * g.k * S
                draw_icon(d, icon, tx, ty - 7 * g.k * S, s, ink, 1.6 * g.k * S)
                _text_center(d, (tx, ty + 10 * g.k * S), txt, f, ink)
            else:
                f = _fit_font(d, label, max_w, int((15 if n <= 8 else 13) * g.k * S))
                _text_center(d, (tx, ty), label, f, ink)

    # центр: кольцо + белая таблетка
    rh = g.R_HOLE * S
    d.ellipse((cx - rh, cy - rh, cx + rh, cy + rh), fill=HOLE_RING)
    rp = rh - 6 * S * g.k
    d.ellipse((cx - rp, cy - rp, cx + rp, cy + rp), fill=(250, 250, 251))
    if center_text:
        f = _fit_font(d, center_text, 2 * rp - 18 * S * g.k, int(12 * g.k * S), bold=False)
        tw = d.textlength(center_text, font=f)
        pw = max(tw + 22 * S * g.k, 62 * S * g.k)
        ph = 28 * S * g.k
        d.rounded_rectangle((cx - pw / 2 - S, cy - ph / 2 - S, cx + pw / 2 + S, cy + ph / 2 + S),
                            radius=ph / 2 + S, fill=PILL_EDGE)
        d.rounded_rectangle((cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2), radius=ph / 2, fill=PILL)
        _text_center(d, (cx, cy), center_text, f, INK_SOFT)

    # подпись под колесом («Конвертировать в PNG»)
    if caption:
        f = _fit_font(d, caption, W - 16 * S, int(11 * g.k * S), bold=False)
        tw = d.textlength(caption, font=f)
        ph = 24 * S * g.k
        y = (g.W + g.CAPTION_H / 2) * S
        d.rounded_rectangle((W / 2 - tw / 2 - 12 * S * g.k, y - ph / 2, W / 2 + tw / 2 + 12 * S * g.k, y + ph / 2),
                            radius=ph / 2, fill=PILL)
        _text_center(d, (W / 2, y), caption, f, CAPTION_INK)

    return im.resize((g.W, g.H), Image.LANCZOS)


# ───────────────────────── мандарин (дольки с градиентом) ─────────────────────────

FLESH = ((255, 221, 165), (255, 152, 54), (243, 125, 24))     # сердцевина · середина · край
FLESH_HI = ((255, 142, 60), (255, 95, 14), (228, 76, 4))      # выбранная долька
MEMBRANE = (255, 244, 230)                                    # плёнка между дольками
PEEL_RING = (255, 184, 110)
PITH = (255, 247, 239)
LABEL_INK = (120, 60, 12)
CORE_INK = (176, 122, 66)
VEIN = (255, 255, 255)


@lru_cache(maxsize=8)
def _flesh_imgs(W, H, cx, cy, R):
    """Два RGB-полотна с радиальным мандариновым градиентом: обычное и для выбранной дольки."""
    base = Image.new("L", (W, H), 255)
    grad = Image.radial_gradient("L").resize((2 * R, 2 * R))   # 0 в центре → 255 к краю
    base.paste(grad, (cx - R, cy - R))

    def col(stops):
        return ImageOps.colorize(base, stops[0], stops[2], stops[1]).convert("RGB")
    return col(FLESH), col(FLESH_HI)


def render_mandarin(labels: list[str], hover: int, center_text: str, caption: str = "",
                    icons: list[str | None] | None = None, scale: float = 1.0) -> Image.Image:
    """Колесо-мандарин: дольки с сочным градиентом вместо плиток. Та же геометрия, что у render_wheel."""
    g = Geometry(scale)
    S = SS
    W, H = g.W * S, g.H * S
    im = Image.new("RGB", (W, H), KEY)
    d = ImageDraw.Draw(im)
    cx, cy = g.CX * S, g.CY * S
    R = g.R_DISC * S
    flesh, hi = _flesh_imgs(W, H, int(round(cx)), int(round(cy)), int(round(R)))

    # тонкий ободок-кожура
    rw = max(1, int(round(3 * g.k * S)))
    d.ellipse((cx - R + rw, cy - R + rw, cx + R - rw, cy + R - rw), outline=PEEL_RING, width=rw)

    n = len(labels)
    if n:
        step = 360 / n
        for i, label in enumerate(labels):
            a0 = i * step - step / 2
            a1 = a0 + step
            sel = i == hover
            # плёнка-мембрана (шире дольки)
            m = Image.new("L", (W, H), 0)
            _tile_shape(ImageDraw.Draw(m), cx, cy, g.R_IN * S - 2 * S, g.R_OUT * S + 2 * S, a0, a1,
                        g.GAP * S - 3 * S, g.CORNER * S + 2 * S, 255)
            im.paste(MEMBRANE, (0, 0), m)
            # мякоть дольки через маску
            fm = Image.new("L", (W, H), 0)
            _tile_shape(ImageDraw.Draw(fm), cx, cy, g.R_IN * S, g.R_OUT * S, a0, a1, g.GAP * S, g.CORNER * S, 255)
            im.paste(hi if sel else flesh, (0, 0), fm)
            mid = a0 + step / 2
            # короткая прожилка у сердцевины (не доходит до подписи)
            if not sel:
                x1, y1 = _polar(cx, cy, g.R_IN * S + (g.R_OUT - g.R_IN) * S * 0.06, mid)
                x2, y2 = _polar(cx, cy, g.R_IN * S + (g.R_OUT - g.R_IN) * S * 0.22, mid)
                d.line((x1, y1, x2, y2), fill=VEIN, width=max(1, int(round(1.3 * g.k * S))))
            # подпись
            rr = (g.R_IN + g.R_OUT) / 2 * S + 2 * S * g.k
            tx, ty = _polar(cx, cy, rr, mid)
            chord = 2 * rr * math.sin(math.radians(step / 2)) - 2 * g.GAP * S - 6 * S * g.k
            max_w = min(chord, (g.R_OUT - g.R_IN) * S * 1.05)
            ink = WHITE if sel else LABEL_INK
            icon = icons[i] if icons else None
            if icon:
                txt = label.upper()
                f = _fit_font(d, txt, max_w, int(10 * g.k * S))
                draw_icon(d, icon, tx, ty - 7 * g.k * S, 7.5 * g.k * S, ink, 1.6 * g.k * S)
                _text_center(d, (tx, ty + 10 * g.k * S), txt, f, ink)
            else:
                f = _fit_font(d, label, max_w, int((15 if n <= 8 else 13) * g.k * S))
                _text_center(d, (tx, ty), label, f, ink)

    # сердцевина
    rh = g.R_HOLE * S
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse((cx - rh, cy - rh, cx + rh, cy + rh), fill=255)
    im.paste(MEMBRANE, (0, 0), m)
    rp = rh - 4 * S * g.k
    d.ellipse((cx - rp, cy - rp, cx + rp, cy + rp), fill=PITH)
    if center_text:
        f = _fit_font(d, center_text, 2 * rp - 18 * S * g.k, int(12 * g.k * S), bold=False)
        _text_center(d, (cx, cy), center_text, f, CORE_INK)

    # подпись под колесом
    if caption:
        f = _fit_font(d, caption, W - 16 * S, int(11 * g.k * S), bold=False)
        tw = d.textlength(caption, font=f)
        ph = 24 * S * g.k
        y = (g.W + g.CAPTION_H / 2) * S
        d.rounded_rectangle((W / 2 - tw / 2 - 12 * S * g.k, y - ph / 2, W / 2 + tw / 2 + 12 * S * g.k, y + ph / 2),
                            radius=ph / 2, fill=PILL)
        _text_center(d, (W / 2, y), caption, f, CAPTION_INK)

    return im.resize((g.W, g.H), Image.LANCZOS)


def _wrap(d, text, fnt, max_w, max_lines=4):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            cand = (cur + " " + word).strip()
            if d.textlength(cand, font=fnt) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def card_size(subtitle: str, scale: float = 1.0, link: str = "") -> tuple[int, int, int]:
    k = scale
    w = round(320 * k)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = _wrap(probe, subtitle, font(int(10 * k * SS), False), (w - 34 * k) * SS)
    extra = (len(lines) - 1) * 15 * k
    h = round(80 * k + extra + (20 * k if link else 0))
    return w, h, len(lines)


def render_card(title: str, subtitle: str, progress: float | None, scale: float = 1.0,
                error: bool = False, link: str = "") -> Image.Image:
    """Карточка прогресса в стиле Tangerine: «× Конвертирую в MP3 / файл · полоса»."""
    S, k = SS, scale
    w, h, _ = card_size(subtitle, scale, link)
    im = Image.new("RGB", (w * S, h * S), KEY)
    d = ImageDraw.Draw(im)
    r = 14 * k * S
    d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=r, fill=(212, 212, 216))
    d.rounded_rectangle((S, S, w * S - 1 - S, h * S - 1 - S), radius=r - S, fill=(250, 250, 251))
    pad = 16 * k * S
    x0, y0 = pad + 5 * k * S, 22 * k * S
    lw = max(1, int(1.6 * k * S))
    d.line((x0 - 4 * k * S, y0 - 4 * k * S, x0 + 4 * k * S, y0 + 4 * k * S), fill=(140, 140, 150), width=lw)
    d.line((x0 - 4 * k * S, y0 + 4 * k * S, x0 + 4 * k * S, y0 - 4 * k * S), fill=(140, 140, 150), width=lw)
    d.text((pad + 20 * k * S, y0), title, font=_fit_font(d, title, (w - 60 * k) * S, int(12 * k * S), True),
           fill=(205, 45, 50) if error else (55, 55, 64), anchor="lm")
    f = font(int(10 * k * S), False)
    lines = _wrap(d, subtitle, f, (w - 34 * k) * S)
    y = 44 * k * S
    for line in lines:
        d.text((pad, y), line, font=f, fill=(125, 125, 135), anchor="lm")
        y += 15 * k * S
    by = y + 3 * k * S
    bh = 4 * k * S
    d.rounded_rectangle((pad, by, w * S - pad, by + bh), radius=bh / 2, fill=(228, 228, 232))
    if progress is not None and progress > 0:
        x1 = pad + (w * S - 2 * pad) * min(1.0, progress)
        d.rounded_rectangle((pad, by, max(pad + bh, x1), by + bh), radius=bh / 2,
                            fill=(229, 72, 77) if error else (255, 110, 50))
    if link:
        d.text((pad, by + 19 * k * S), link, font=font(int(10 * k * S), True), fill=(235, 85, 10), anchor="lm")
    return im.resize((w, h), Image.LANCZOS)


def card_bar_y(subtitle: str, scale: float = 1.0) -> float:
    _, _, n = card_size(subtitle, scale)
    return (44 + (n - 1) * 15 + 15 + 3) * scale


def render_card_indeterminate(title, subtitle, phase: float, scale: float = 1.0) -> Image.Image:
    """Карточка с «бегущей» полосой, пока процент неизвестен."""
    im = render_card(title, subtitle, None, scale)
    d = ImageDraw.Draw(im)
    k = scale
    w = im.width
    pad, by, bh = 16 * k, card_bar_y(subtitle, scale), 4 * k
    span = (w - 2 * pad) * 0.3
    x = pad + ((w - 2 * pad) + span) * (phase % 1.0) - span
    a, b = max(pad, x), min(w - pad, x + span)
    if b > a:
        d.rounded_rectangle((a, by, b, by + bh), radius=bh / 2, fill=(255, 120, 70))
    return im
