"""
Mandarin — функции уровня Acrobat Pro (офлайн).

Каждая операция существует в двух видах:
  * над открытым документом PyMuPDF (для редактора viewer.py) — меняет doc на месте;
  * над файлом (для колеса и главного окна) — сохраняет результат рядом с оригиналом.

Этапы:
  1. защита паролем / снятие, штампы и подпись-картинка (для редактора);
  2. зачернение по шаблонам, водяной знак, номера страниц, колонтитулы, Bates, обрезка полей,
     экспорт в Excel и PowerPoint;
  3. OCR (распознавание сканов, встроенный в PyMuPDF Tesseract) и сравнение двух PDF;
  4. формы (список/заполнение/сглаживание), подпись сертификатом (pyHanko), PDF/A (Ghostscript).
"""
from __future__ import annotations

import datetime as _dt
import difflib
import glob
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from engine import EngineError, Result, _fitz, _run, target, unique

# ───────────────────────── общие вещи ─────────────────────────

def cyr_fontfile() -> str | None:
    """TTF-шрифт с кириллицей для текста, который мы вписываем в PDF."""
    win = os.environ.get("WINDIR", r"C:\Windows")
    for p in (os.path.join(win, "Fonts", "arial.ttf"), os.path.join(win, "Fonts", "segoeui.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(p):
            return p
    return None


def cyr_bold_fontfile() -> str | None:
    win = os.environ.get("WINDIR", r"C:\Windows")
    for p in (os.path.join(win, "Fonts", "arialbd.ttf"), os.path.join(win, "Fonts", "segoeuib.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return p
    return cyr_fontfile()


def open_pdf(path: Path, password: str | None = None):
    doc = _fitz().open(path)
    if doc.needs_pass:      # после authenticate needs_pass больше не трогаем (см. unlock)
        if not password or not doc.authenticate(password):
            doc.close()
            raise EngineError("PDF защищён паролем — введите пароль")
    return doc


def save_doc(doc, dst: Path, **kw) -> Path:
    opts = dict(garbage=3, deflate=True)
    opts.update(kw)
    doc.save(dst, **opts)
    return dst


def _html_escape(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>"))


def text_box(page, rect, text, size=10, color=(0, 0, 0), bold=False, align="left", font="sans-serif",
             visible=True):
    """Текст в рамке через insert_htmlbox: кириллица, выравнивание, и Ghostscript/PDF-A не портит шрифт.
    visible=True — rect в координатах видимой (повёрнутой) страницы; False — уже в координатах PDF."""
    fitz = _fitz()
    r, g, b = (int(c * 255) if c <= 1 else int(c) for c in color)
    css = (f"* {{font-family: {font}; font-size: {size}px; color: rgb({r},{g},{b}); "
           f"font-weight: {'bold' if bold else 'normal'}; text-align: {align}; margin: 0; padding: 0; "
           f"line-height: 1.15;}}")
    rect = fitz.Rect(rect)
    rot = page.rotation
    if rot and visible:
        rect = rect * page.derotation_matrix
        rect.normalize()
    page.insert_htmlbox(rect, _html_escape(text), css=css, rotate=rot)


def _insert_text(page, point, text, size=10, color=(0, 0, 0), bold=False, rotate=0):
    """Текст от точки (как у insert_text: point — базовая линия)."""
    x, y = point
    w = _text_width(text, size, bold) + size * 2
    text_box(page, (x, y - size * 1.05, x + w, y + size * 0.6), text, size, color, bold)


def _text_width(text, size, bold=False) -> float:
    try:
        return _fitz().Font("helv" if not bold else "hebo").text_length(text, fontsize=size) * 1.08
    except Exception:
        return len(text) * size * 0.6


def _png_bytes(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _pil_font(px, bold=False):
    from PIL import ImageFont
    path = cyr_bold_fontfile() if bold else cyr_fontfile()
    try:
        return ImageFont.truetype(path, px) if path else ImageFont.load_default(px)
    except Exception:
        return ImageFont.load_default()


def _parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-3,5' → [0,1,2,4]; пусто — все страницы."""
    if not spec or not str(spec).strip():
        return list(range(n))
    out = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
            out.extend(range(a - 1, min(b, n)))
        else:
            i = int(part) - 1
            if 0 <= i < n:
                out.append(i)
    return sorted(set(i for i in out if 0 <= i < n))


# ═════════════════════════ ЭТАП 1: защита, штампы, подпись-картинка ═════════════════════════

def protect(src: Path, user_pw: str, owner_pw: str | None = None, allow_print=True, allow_copy=False,
            allow_edit=False, password: str | None = None) -> Result:
    fitz = _fitz()
    if not user_pw and not owner_pw:
        raise EngineError("Укажите пароль")
    doc = open_pdf(src, password)
    perm = fitz.PDF_PERM_ACCESSIBILITY
    if allow_print:
        perm |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
    if allow_copy:
        perm |= fitz.PDF_PERM_COPY
    if allow_edit:
        perm |= fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ANNOTATE | fitz.PDF_PERM_FORM | fitz.PDF_PERM_ASSEMBLE
    dst = target(src, ".pdf", " (защищён)")
    save_doc(doc, dst, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=user_pw or "",
             owner_pw=owner_pw or user_pw, permissions=perm)
    doc.close()
    return Result([dst], "Пароль установлен (AES-256)")


def unlock(src: Path, password: str) -> Result:
    fitz = _fitz()
    doc = fitz.open(src)
    # ВАЖНО: needs_pass читаем только до authenticate — повторное обращение после ввода пароля
    # сбрасывает расшифровку в MuPDF, и в сохранённом файле пропадает текст.
    locked = doc.needs_pass
    encrypted = doc.is_encrypted
    if locked and not doc.authenticate(password or ""):
        doc.close()
        raise EngineError("Неверный пароль")
    if not locked and not encrypted:
        doc.close()
        raise EngineError("Этот PDF не защищён паролем")
    dst = target(src, ".pdf", " (без пароля)")
    save_doc(doc, dst, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()
    return Result([dst], "Защита снята")


STAMPS = {
    "УТВЕРЖДЕНО": (0, 128, 64), "СОГЛАСОВАНО": (0, 110, 200), "КОПИЯ ВЕРНА": (40, 40, 160),
    "ОПЛАЧЕНО": (0, 128, 64), "ЧЕРНОВИК": (200, 120, 0), "ОТКЛОНЕНО": (200, 30, 40),
    "КОНФИДЕНЦИАЛЬНО": (200, 30, 40), "ПОЛУЧЕНО": (40, 40, 160),
}


def make_stamp_png(text: str, color=(200, 30, 40), date: bool = True) -> bytes:
    """Прямоугольный штамп в рамке (как в Acrobat), прозрачный фон."""
    from PIL import Image, ImageDraw
    S = 4
    f1 = _pil_font(40 * S, True)
    f2 = _pil_font(18 * S)
    sub = _dt.date.today().strftime("%d.%m.%Y") if date else ""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    w1 = probe.textlength(text, font=f1)
    w2 = probe.textlength(sub, font=f2) if sub else 0
    W = int(max(w1, w2) + 60 * S)
    H = int((92 if sub else 70) * S)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    col = tuple(color) + (235,)
    d.rounded_rectangle((3 * S, 3 * S, W - 3 * S, H - 3 * S), radius=12 * S, outline=col, width=5 * S)
    d.rounded_rectangle((10 * S, 10 * S, W - 10 * S, H - 10 * S), radius=8 * S, outline=col, width=2 * S)
    d.text((W / 2, (36 if sub else H / S / 2) * S), text, font=f1, fill=col, anchor="mm")
    if sub:
        d.text((W / 2, 68 * S), sub, font=f2, fill=col, anchor="mm")
    return _png_bytes(im.resize((W // 2, H // 2), Image.LANCZOS))


def insert_png(page, rect, png: bytes, rotate=None):
    """rect — в координатах PDF; на повёрнутой странице картинка выравнивается по видимой ориентации."""
    page.insert_image(rect, stream=png, keep_proportion=True, overlay=True,
                      rotate=page.rotation if rotate is None else rotate)


def png_size(png: bytes) -> tuple[int, int]:
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


# ═════════════════════════ ЭТАП 2 ═════════════════════════

REDACT_PATTERNS = {
    "email": ("Электронная почта", r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": ("Телефоны", r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b"),
    "passport": ("Паспорт РФ (серия и номер)", r"\b\d{2}\s?\d{2}\s?№?\s?\d{6}\b"),
    "inn": ("ИНН", r"(?<!\d)(?:\d{12}|\d{10})(?!\d)"),
    "snils": ("СНИЛС", r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b"),
    "card": ("Номера банковских карт", r"\b(?:\d{4}[\s-]?){3}\d{4}\b"),
    "account": ("Расчётные счета (20 цифр)", r"(?<!\d)\d{20}(?!\d)"),
    "date": ("Даты", r"\b\d{2}\.\d{2}\.\d{4}\b"),
}


def find_sensitive(doc, keys: list[str], words: list[str] | None = None, regex: str | None = None):
    """[(номер_страницы, Rect)] для всех найденных совпадений."""
    pats = [REDACT_PATTERNS[k][1] for k in keys if k in REDACT_PATTERNS]
    if regex:
        pats.append(regex)
    hits = []
    for pno, page in enumerate(doc):
        text = page.get_text("text")
        found = set()
        for p in pats:
            for m in re.finditer(p, text, flags=re.IGNORECASE):
                s = m.group(0).strip()
                if len(s) >= 3:
                    found.add(s)
        for w in words or []:
            w = w.strip()
            if w:
                found.add(w)
        for s in found:
            # совпадение может разрываться переносом строки — ищем и по частям
            rects = page.search_for(s)
            if not rects and "\n" in s:
                for part in s.split("\n"):
                    rects += page.search_for(part.strip())
            hits.extend((pno, r) for r in rects)
    return hits


def apply_redactions(doc, hits, fill=(0, 0, 0)) -> int:
    pages = {}
    for pno, r in hits:
        doc[pno].add_redact_annot(r, fill=fill)
        pages[pno] = True
    for pno in pages:
        doc[pno].apply_redactions(images=_fitz().PDF_REDACT_IMAGE_PIXELS)
    return len(hits)


def redact_file(src: Path, keys: list[str], words: list[str] | None = None, regex: str | None = None) -> Result:
    doc = open_pdf(src)
    hits = find_sensitive(doc, keys, words, regex)
    if not hits:
        doc.close()
        raise EngineError("Ничего не найдено для зачернения")
    n = apply_redactions(doc, hits)
    doc.set_metadata({})
    dst = target(src, ".pdf", " (зачернён)")
    save_doc(doc, dst, garbage=4, clean=True)
    doc.close()
    return Result([dst], f"Зачернено фрагментов: {n} (текст удалён из файла)")


def watermark_png(text: str, size_px=96, color=(200, 30, 40), opacity=0.18, angle=45):
    from PIL import Image, ImageDraw
    f = _pil_font(size_px, True)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    w = int(probe.textlength(text, font=f)) + 40
    h = int(size_px * 1.5)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((w / 2, h / 2), text, font=f, fill=tuple(color) + (int(255 * opacity),), anchor="mm")
    return _png_bytes(im.rotate(angle, expand=True, resample=Image.BICUBIC))


def watermark_doc(doc, text: str, opacity=0.18, angle=45, color=(200, 30, 40), pages: str | None = None,
                  scale=0.8):
    png = watermark_png(text, 110, color, opacity, angle)
    pw, ph = png_size(png)
    for i in _parse_pages(pages, len(doc)):
        page = doc[i]
        r = page.rect
        s = min(r.width * scale / pw, r.height * scale / ph)
        w, h = pw * s, ph * s
        rect = _fitz().Rect(r.x0 + (r.width - w) / 2, r.y0 + (r.height - h) / 2,
                            r.x0 + (r.width + w) / 2, r.y0 + (r.height + h) / 2)
        insert_png(page, rect, png)


def watermark_file(src: Path, text: str, opacity=0.18, angle=45, color=(200, 30, 40), pages=None) -> Result:
    doc = open_pdf(src)
    watermark_doc(doc, text, opacity, angle, color, pages)
    dst = target(src, ".pdf", " (водяной знак)")
    save_doc(doc, dst)
    doc.close()
    return Result([dst])


POSITIONS = {
    "bottom-center": "Снизу по центру", "bottom-right": "Снизу справа", "bottom-left": "Снизу слева",
    "top-center": "Сверху по центру", "top-right": "Сверху справа", "top-left": "Сверху слева",
}


def _place(page, text, position, size, margin=28, bold=False):
    r = page.rect
    vert, horiz = position.split("-")
    h = size * 1.6
    y0 = r.y0 + margin - size * 0.4 if vert == "top" else r.y1 - margin - size * 0.9
    align = {"left": "left", "right": "right", "center": "center"}[horiz]
    text_box(page, (r.x0 + margin, y0, r.x1 - margin, y0 + h), text, size, (0.15, 0.15, 0.18), bold, align)


def page_numbers_doc(doc, fmt="Стр. {n} из {total}", position="bottom-center", start=1, size=10,
                     pages=None, skip_first=False):
    idx = _parse_pages(pages, len(doc))
    if skip_first and idx and idx[0] == 0:
        idx = idx[1:]
    total = len(idx) + start - 1
    for k, i in enumerate(idx):
        _place(doc[i], fmt.format(n=start + k, total=total), position, size)


def header_footer_doc(doc, header="", footer="", size=9, file_name=""):
    today = _dt.date.today().strftime("%d.%m.%Y")
    n = len(doc)
    for i, page in enumerate(doc):
        vals = dict(n=i + 1, total=n, date=today, file=file_name)
        if header:
            _place(page, header.format(**vals), "top-center", size)
        if footer:
            _place(page, footer.format(**vals), "bottom-center", size)


def bates_files(paths: list[Path], prefix="DOC-", start=1, digits=6, position="bottom-right", size=9) -> Result:
    """Сквозная нумерация Бейтса по нескольким файлам (юридический документооборот)."""
    res = Result()
    num = start
    first = last = ""
    for p in paths:
        doc = open_pdf(p)
        for page in doc:
            label = f"{prefix}{num:0{digits}d}"
            first = first or label
            last = label
            _place(page, label, position, size, bold=True)
            num += 1
        dst = target(p, ".pdf", " (Bates)")
        save_doc(doc, dst)
        doc.close()
        res.outputs.append(dst)
    res.message = f"Номера {first} … {last}"
    return res


def numbers_file(src: Path, **kw) -> Result:
    doc = open_pdf(src)
    page_numbers_doc(doc, **kw)
    dst = target(src, ".pdf", " (нумерация)")
    save_doc(doc, dst)
    doc.close()
    return Result([dst])


def header_footer_file(src: Path, header="", footer="", size=9) -> Result:
    doc = open_pdf(src)
    header_footer_doc(doc, header, footer, size, src.name)
    dst = target(src, ".pdf", " (колонтитулы)")
    save_doc(doc, dst)
    doc.close()
    return Result([dst])


def content_bbox(page):
    """Рамка всего содержимого страницы (текст, картинки, графика)."""
    fitz = _fitz()
    r = fitz.Rect()
    try:
        for kind, bbox in page.get_bboxlog():
            if kind.startswith("ignore"):
                continue
            b = fitz.Rect(bbox)
            if b.is_valid and not b.is_empty and b.width < page.rect.width * 1.5:
                r |= b
    except Exception:
        for b in page.get_text("blocks"):
            r |= fitz.Rect(b[:4])
    return r & page.rect if not r.is_empty else page.rect


def crop_doc(doc, mode="auto", margin_mm=5.0, pages=None):
    fitz = _fitz()
    pad = margin_mm * 72 / 25.4
    for i in _parse_pages(pages, len(doc)):
        page = doc[i]
        mb = page.mediabox
        if mode == "auto":
            box = content_bbox(page)
            box = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad) & page.rect
        else:  # срезать одинаковые поля margin_mm со всех сторон
            r = page.rect
            box = fitz.Rect(r.x0 + pad, r.y0 + pad, r.x1 - pad, r.y1 - pad)
        if box.width < 20 or box.height < 20:
            continue
        # cropbox задаётся в непомеченных (unrotated) координатах
        box = box * page.derotation_matrix
        box.normalize()
        page.set_cropbox(box & mb)


def crop_file(src: Path, mode="auto", margin_mm=5.0) -> Result:
    doc = open_pdf(src)
    crop_doc(doc, mode, margin_mm)
    dst = target(src, ".pdf", " (обрезан)")
    save_doc(doc, dst)
    doc.close()
    return Result([dst])


def export_xlsx(paths: list[Path]) -> Result:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    res = Result()
    for src in paths:
        doc = open_pdf(src)
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        n_tables = 0
        for pno, page in enumerate(doc, 1):
            try:
                tabs = page.find_tables()
                tables = list(tabs.tables) if hasattr(tabs, "tables") else list(tabs)
            except Exception:
                tables = []
            for t in tables:
                rows = t.extract()
                if not rows or len(rows) < 2 and len(rows[0]) < 2:
                    continue
                n_tables += 1
                ws = wb.create_sheet(f"Стр{pno}-Табл{n_tables}"[:31])
                for r_i, row in enumerate(rows, 1):
                    for c_i, val in enumerate(row, 1):
                        cell = ws.cell(r_i, c_i, _cell_value(val))
                        cell.alignment = Alignment(wrap_text=True, vertical="top")
                        if r_i == 1:
                            cell.font = Font(bold=True)
                            cell.fill = PatternFill("solid", fgColor="FFF1E8")
                for c_i in range(1, (max(len(r) for r in rows) if rows else 0) + 1):
                    width = max((len(str(r[c_i - 1] or "")) for r in rows if len(r) >= c_i), default=8)
                    ws.column_dimensions[openpyxl.utils.get_column_letter(c_i)].width = min(60, max(8, width + 2))
                ws.freeze_panes = "A2"
        if n_tables == 0:
            # таблиц нет — выгружаем текст построчно, чтобы файл не был пустым
            ws = wb.create_sheet("Текст")
            r_i = 1
            for pno, page in enumerate(doc, 1):
                for line in page.get_text("text").splitlines():
                    if line.strip():
                        ws.cell(r_i, 1, pno)
                        ws.cell(r_i, 2, line)
                        r_i += 1
            ws.column_dimensions["B"].width = 100
        doc.close()
        dst = target(src, ".xlsx")
        wb.save(dst)
        res.outputs.append(dst)
        res.message = f"Таблиц найдено: {n_tables}" if n_tables else "Таблиц не найдено — выгружен текст"
    return res


def _cell_value(v):
    if v is None:
        return None
    s = str(v).replace("\n", " ").strip()
    t = s.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if re.fullmatch(r"-?\d+(\.\d+)?", t) and len(t) < 16 and not (len(t) > 1 and t[0] == "0" and t[1] != "."):
        return float(t) if "." in t else int(t)
    return s


def export_pptx(paths: list[Path]) -> Result:
    from pptx import Presentation
    from pptx.util import Emu
    res = Result()
    for src in paths:
        doc = open_pdf(src)
        prs = Presentation()
        r0 = doc[0].rect
        prs.slide_width = Emu(int(r0.width * 12700))
        prs.slide_height = Emu(int(r0.height * 12700))
        blank = prs.slide_layouts[6]
        for page in doc:
            slide = prs.slides.add_slide(blank)
            pix = page.get_pixmap(dpi=170)
            img = io.BytesIO(pix.tobytes("png"))
            sw, sh = prs.slide_width, prs.slide_height
            pr = page.rect
            k = min(sw / (pr.width * 12700), sh / (pr.height * 12700))
            w, h = int(pr.width * 12700 * k), int(pr.height * 12700 * k)
            slide.shapes.add_picture(img, (sw - w) // 2, (sh - h) // 2, w, h)
            notes = page.get_text("text").strip()
            if notes:
                slide.notes_slide.notes_text_frame.text = notes[:3000]
        doc.close()
        dst = target(src, ".pptx")
        prs.save(dst)
        res.outputs.append(dst)
    res.message = "Текст страниц — в заметках к слайдам"
    return res


# ═════════════════════════ ЭТАП 3: OCR и сравнение ═════════════════════════

def tessdata_dir() -> str | None:
    """Папка с языками OCR: рядом с программой, затем переменная TESSDATA_PREFIX, затем Tesseract."""
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    cands = [here / "tessdata", Path(sys.executable).parent / "tessdata", Path(__file__).resolve().parent / "tessdata"]
    if os.environ.get("TESSDATA_PREFIX"):
        cands.append(Path(os.environ["TESSDATA_PREFIX"]))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            cands.append(Path(base) / "Tesseract-OCR" / "tessdata")
    cands += [Path("/usr/share/tesseract-ocr/5/tessdata"), Path("/usr/share/tessdata")]
    for c in cands:
        if c.is_dir() and any(c.glob("*.traineddata")):
            return str(c)
    return None


def ocr_languages() -> str:
    td = tessdata_dir()
    if not td:
        return ""
    have = {Path(p).stem for p in glob.glob(os.path.join(td, "*.traineddata"))}
    langs = [l for l in ("rus", "eng") if l in have] or sorted(have)[:2]
    return "+".join(langs)


def ocr_available() -> bool:
    return bool(tessdata_dir())


def page_needs_ocr(page) -> bool:
    return len(page.get_text("text").strip()) < 25 and bool(page.get_images())


def ocr_file(src: Path, dpi=300, force=False, progress=None) -> Result:
    """Делает PDF с поиском и копированием текста. Страницы, где текст уже есть, не трогаются."""
    td = tessdata_dir()
    if not td:
        raise EngineError("Нет языковых файлов OCR (папка tessdata рядом с программой)")
    fitz = _fitz()
    langs = ocr_languages()
    doc = open_pdf(src)
    out = fitz.open()
    done = 0
    for i, page in enumerate(doc):
        if progress:
            progress(i / len(doc))
        if force or page_needs_ocr(page):
            pix = page.get_pixmap(dpi=dpi)
            data = pix.pdfocr_tobytes(language=langs, tessdata=td)
            with fitz.open("pdf", data) as one:
                one[0].set_mediabox(one[0].rect)
                out.insert_pdf(one)
            # возвращаем исходный размер страницы (pixmap в точках = dpi/72 крупнее)
            p = out[len(out) - 1]
            p.set_mediabox(fitz.Rect(0, 0, page.rect.width * dpi / 72, page.rect.height * dpi / 72))
            done += 1
        else:
            out.insert_pdf(doc, from_page=i, to_page=i)
    doc.close()
    if done == 0:
        out.close()
        raise EngineError("В документе уже есть текстовый слой — распознавать нечего")
    # уменьшаем распознанные страницы до исходного размера
    _normalize_page_sizes(out, src)
    dst = target(src, ".pdf", " (распознан)")
    save_doc(out, dst, garbage=4)
    out.close()
    return Result([dst], f"Распознано страниц: {done} · язык: {langs}")


def _normalize_page_sizes(doc, src: Path):
    """pdfocr создаёт страницу размером с картинку; приводим её к размеру исходной страницы."""
    fitz = _fitz()
    with fitz.open(src) as orig:
        sizes = [p.rect for p in orig]
    for i, page in enumerate(doc):
        if i >= len(sizes):
            break
        want = sizes[i]
        if abs(page.rect.width - want.width) < 1 and abs(page.rect.height - want.height) < 1:
            continue
        # переносим страницу в новую нужного размера через show_pdf_page
    tmp = fitz.open()
    for i, page in enumerate(doc):
        want = sizes[i] if i < len(sizes) else page.rect
        if abs(page.rect.width - want.width) < 1 and abs(page.rect.height - want.height) < 1:
            tmp.insert_pdf(doc, from_page=i, to_page=i)
        else:
            np_ = tmp.new_page(width=want.width, height=want.height)
            np_.show_pdf_page(np_.rect, doc, i)
    doc.delete_pages(range(len(doc)))
    doc.insert_pdf(tmp)
    tmp.close()


def ocr_doc_text(doc, pno: int) -> str:
    """Текст страницы с OCR (для поиска/копирования в редакторе)."""
    td = tessdata_dir()
    if not td:
        raise EngineError("Нет языковых файлов OCR")
    tp = doc[pno].get_textpage_ocr(language=ocr_languages(), tessdata=td, dpi=300, full=True)
    return doc[pno].get_text(textpage=tp)


def _words(doc):
    out = []
    for pno, page in enumerate(doc):
        for w in page.get_text("words", sort=True):
            out.append((w[4], pno, _fitz().Rect(w[:4])))
    return out


def compare_files(a: Path, b: Path) -> Result:
    """Отчёт о различиях: страницы «было/стало» рядом, удалённое — красным, добавленное — зелёным."""
    fitz = _fitz()
    da, db = open_pdf(a), open_pdf(b)
    wa, wb = _words(da), _words(db)
    sm = difflib.SequenceMatcher(None, [w[0] for w in wa], [w[0] for w in wb], autojunk=False)
    removed, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(wa[i1:i2])
        if op in ("replace", "insert"):
            added.extend(wb[j1:j2])
    out = fitz.open()
    # титульная сводка
    cover = out.new_page(width=842, height=595)
    _insert_text(cover, (40, 60), "Сравнение документов", size=22, bold=True)
    _insert_text(cover, (40, 100), f"Было:   {a.name}  ({len(da)} стр.)", size=12)
    _insert_text(cover, (40, 122), f"Стало:  {b.name}  ({len(db)} стр.)", size=12)
    _insert_text(cover, (40, 160), f"Удалено слов: {len(removed)}", size=13, color=(0.8, 0.1, 0.15))
    _insert_text(cover, (40, 182), f"Добавлено слов: {len(added)}", size=13, color=(0.05, 0.55, 0.25))
    y = 220
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or y > 560:
            continue
        old = " ".join(w[0] for w in wa[i1:i2])[:70]
        new = " ".join(w[0] for w in wb[j1:j2])[:70]
        pg = (wb[j1][1] + 1) if j1 < len(wb) else (wa[i1][1] + 1 if i1 < len(wa) else "?")
        line = f"стр. {pg}:  " + (f"«{old}» → «{new}»" if op == "replace" else
                                   f"удалено «{old}»" if op == "delete" else f"добавлено «{new}»")
        _insert_text(cover, (40, y), line[:120], size=10)
        y += 16
    # постраничное сравнение
    rem_by = {}
    for w in removed:
        rem_by.setdefault(w[1], []).append(w[2])
    add_by = {}
    for w in added:
        add_by.setdefault(w[1], []).append(w[2])
    n = max(len(da), len(db))
    for i in range(n):
        ra = da[min(i, len(da) - 1)].rect
        rb = db[min(i, len(db) - 1)].rect
        H = max(ra.height, rb.height) + 40
        W = ra.width + rb.width + 30
        page = out.new_page(width=W, height=H)
        if i < len(da):
            box = fitz.Rect(10, 30, 10 + ra.width, 30 + ra.height)
            page.show_pdf_page(box, da, i)
            for r in rem_by.get(i, []):
                hl = page.add_highlight_annot(fitz.Rect(r.x0 + box.x0, r.y0 + box.y0, r.x1 + box.x0, r.y1 + box.y0))
                hl.set_colors(stroke=(1, 0.45, 0.45))
                hl.update()
        if i < len(db):
            box = fitz.Rect(20 + ra.width, 30, 20 + ra.width + rb.width, 30 + rb.height)
            page.show_pdf_page(box, db, i)
            for r in add_by.get(i, []):
                hl = page.add_highlight_annot(fitz.Rect(r.x0 + box.x0, r.y0 + box.y0, r.x1 + box.x0, r.y1 + box.y0))
                hl.set_colors(stroke=(0.45, 0.9, 0.55))
                hl.update()
        _insert_text(page, (12, 20), f"Было — стр. {i + 1}", size=10, bold=True, color=(0.75, 0.1, 0.15))
        _insert_text(page, (22 + ra.width, 20), f"Стало — стр. {i + 1}", size=10, bold=True, color=(0.05, 0.5, 0.25))
    da.close()
    db.close()
    dst = unique(b.with_name(f"Сравнение — {a.stem} и {b.stem}.pdf"))
    save_doc(out, dst)
    out.close()
    msg = "Документы совпадают по тексту" if not removed and not added else \
        f"Удалено слов: {len(removed)}, добавлено: {len(added)}"
    return Result([dst], msg)


# ═════════════════════════ ЭТАП 4: формы, подпись сертификатом, PDF/A ═════════════════════════

def list_fields(doc) -> list[dict]:
    out = []
    for pno, page in enumerate(doc):
        for w in page.widgets():
            out.append({"page": pno, "name": w.field_name, "type": w.field_type_string,
                        "value": w.field_value, "rect": tuple(w.rect),
                        "choices": list(w.choice_values or [])})
    return out


def fill_fields(doc, values: dict) -> int:
    n = 0
    for page in doc:
        for w in page.widgets():
            if w.field_name in values:
                v = values[w.field_name]
                if w.field_type == _fitz().PDF_WIDGET_TYPE_CHECKBOX:
                    w.field_value = w.on_state() if v in (True, "1", "on", "Yes", "да") else "Off"
                else:
                    w.field_value = str(v)
                w.update()
                n += 1
    return n


FIELD_TYPES = {"text": "Текстовое поле", "multiline": "Многострочный текст", "checkbox": "Флажок",
               "combobox": "Выпадающий список", "listbox": "Список", "signature": "Поле для подписи"}


def add_field(page, rect, kind: str, name: str, choices: list[str] | None = None, value: str = ""):
    fitz = _fitz()
    w = fitz.Widget()
    w.rect = fitz.Rect(rect)
    w.field_name = name
    w.text_font = "Helv"
    w.text_fontsize = 0 if kind == "multiline" else 11
    w.border_color = (0.6, 0.6, 0.65)
    w.border_width = 0.8
    w.fill_color = (0.97, 0.97, 1.0)
    if kind in ("text", "multiline"):
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        if kind == "multiline":
            w.field_flags |= fitz.PDF_TX_FIELD_IS_MULTILINE
        w.field_value = value
    elif kind == "checkbox":
        w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        w.field_value = False
    elif kind in ("combobox", "listbox"):
        w.field_type = fitz.PDF_WIDGET_TYPE_COMBOBOX if kind == "combobox" else fitz.PDF_WIDGET_TYPE_LISTBOX
        w.choice_values = choices or ["Вариант 1", "Вариант 2"]
        w.field_value = value or w.choice_values[0]
    elif kind == "signature":
        w.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
    else:
        raise EngineError(f"Неизвестный тип поля: {kind}")
    page.add_widget(w)
    return w


def flatten_doc(doc):
    """«Сгладить»: формы и комментарии становятся частью страницы (их нельзя изменить)."""
    if hasattr(doc, "bake"):
        doc.bake(annots=True, widgets=True)
    else:
        raise EngineError("Нужна более новая версия PyMuPDF (1.23+)")


def flatten_file(src: Path) -> Result:
    doc = open_pdf(src)
    flatten_doc(doc)
    dst = target(src, ".pdf", " (сглажен)")
    save_doc(doc, dst)
    doc.close()
    return Result([dst], "Формы и комментарии вшиты в страницы")


def make_self_signed(dst: Path, name: str, email: str = "", password: str = "", years: int = 3) -> Path:
    """Самоподписанный сертификат (.pfx) — для внутренней подписи документов."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if email:
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    subject = x509.Name(attrs)
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=365 * years))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .sign(key, hashes.SHA256()))
    enc = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    dst.write_bytes(pkcs12.serialize_key_and_certificates(name.encode(), key, cert, None, enc))
    return dst


def sign_with_certificate(src: Path, pfx: Path, pfx_password: str = "", reason: str = "", location: str = "",
                          visible: bool = True, page: int = -1, box: tuple | None = None) -> Result:
    """Цифровая подпись PAdES (pyHanko). Видимая подпись — штамп с именем и датой в углу страницы."""
    try:
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import fields, signers
        from pyhanko.stamp import TextStampStyle
    except ImportError:
        raise EngineError("Нужен пакет pyhanko")
    signer = signers.SimpleSigner.load_pkcs12(pfx_file=str(pfx), passphrase=pfx_password.encode() if pfx_password else None)
    if signer is None:
        raise EngineError("Не удалось открыть сертификат — проверьте пароль")
    cn = signer.signing_cert.subject.native.get("common_name", "Подписант")
    fitz = _fitz()
    with fitz.open(src) as d:
        if d.needs_pass:
            raise EngineError("Сначала снимите пароль с PDF")
        n_pages = len(d)
        pr = d[page if page >= 0 else n_pages - 1].rect
    pidx = page if page >= 0 else n_pages - 1
    field_name = f"Подпись{_dt.datetime.now().strftime('%H%M%S')}"
    dst = target(src, ".pdf", " (подписан)")
    with open(src, "rb") as inf:
        w = IncrementalPdfFileWriter(inf, strict=False)
        if visible:
            if box is None:
                # правый нижний угол; в pyHanko координаты снизу вверх
                bw, bh = 220, 62
                box = (pr.width - bw - 36, 36, pr.width - 36, 36 + bh)
            fields.append_signature_field(w, sig_field_spec=fields.SigFieldSpec(field_name, on_page=pidx, box=box))
        else:
            fields.append_signature_field(w, sig_field_spec=fields.SigFieldSpec(field_name))
        meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason or None,
                                            location=location or None, name=cn)
        font = cyr_fontfile()
        style = None
        if visible:
            kw = dict(stamp_text="Подписано электронной подписью\n%(signer)s\n%(ts)s", border_width=1)
            if font:
                try:
                    from pyhanko.pdf_utils.font.opentype import GlyphAccumulatorFactory
                    kw["text_box_style"] = __import__("pyhanko.pdf_utils.text", fromlist=["TextBoxStyle"]).TextBoxStyle(
                        font=GlyphAccumulatorFactory(font), font_size=8)
                except Exception:
                    pass
            style = TextStampStyle(**kw)
        pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style)
        with open(dst, "wb") as outf:
            pdf_signer.sign_pdf(w, output=outf)
    return Result([dst], f"Подписано: {cn}")


def verify_signatures(src: Path) -> list[dict]:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext
    out = []
    with open(src, "rb") as f:
        r = PdfFileReader(f, strict=False)
        for sig in r.embedded_signatures:
            try:
                ctx = ValidationContext(allow_fetching=False, trust_roots=[sig.signer_cert])
                st = validate_pdf_signature(sig, ctx)
                out.append({"field": sig.field_name, "signer": sig.signer_cert.subject.native.get("common_name", "?"),
                            "intact": st.intact and st.valid, "time": str(st.signer_reported_dt or ""),
                            "covers_all": st.coverage.name if hasattr(st, "coverage") else ""})
            except Exception as e:
                out.append({"field": sig.field_name, "signer": "?", "intact": False, "time": "", "error": str(e)})
    return out


def verify_file(src: Path) -> Result:
    sigs = verify_signatures(src)
    if not sigs:
        raise EngineError("В документе нет цифровых подписей")
    lines = []
    for s in sigs:
        state = "целостна ✓" if s["intact"] else "НАРУШЕНА ✕"
        lines.append(f"{s['signer']}: подпись {state}" + (f", {s['time'][:19]}" if s.get("time") else ""))
    return Result(message="; ".join(lines))


def ghostscript_exe() -> str | None:
    for name in ("gswin64c", "gswin32c", "gs"):
        p = shutil.which(name)
        if p:
            return p
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            found = sorted(glob.glob(os.path.join(base, "gs", "gs*", "bin", "gswin*c.exe")), reverse=True)
            if found:
                return found[0]
    return None


def pdfa_available() -> bool:
    return bool(ghostscript_exe())


def to_pdfa(src: Path, level: int = 2) -> Result:
    gs = ghostscript_exe()
    if not gs:
        raise EngineError("Для PDF/A установите бесплатный Ghostscript (ghostscript.com)")
    from PIL import ImageCms
    tmp = Path(tempfile.mkdtemp(prefix="mandarin-pdfa-"))
    try:
        icc = tmp / "srgb.icc"
        icc.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        defps = tmp / "PDFA_def.ps"
        icc_ps = str(icc).replace("\\", "/")
        defps.write_text(f"""%!
/ICCProfile ({icc_ps}) def
[/_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[{{icc_PDFA}} <</N 3>> /PUT pdfmark
[{{icc_PDFA}} ICCProfile (r) file /PUT pdfmark
[/_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFA}} <<
  /Type /OutputIntent /S /GTS_PDFA1
  /DestOutputProfile {{icc_PDFA}}
  /OutputConditionIdentifier (sRGB)
>> /PUT pdfmark
[{{Catalog}} <</OutputIntents [ {{OutputIntent_PDFA}} ]>> /PUT pdfmark
""", encoding="ascii")
        dst = target(src, ".pdf", f" (PDF-A{level}b)")
        tmp_out = tmp / "out.pdf"
        _run([gs, f"-dPDFA={level}", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dQUIET",
              "-sColorConversionStrategy=RGB", "-sProcessColorModel=DeviceRGB", "-dPDFACompatibilityPolicy=1",
              "-dEmbedAllFonts=true", "-sDEVICE=pdfwrite", f"--permit-file-read={str(tmp).replace(chr(92), '/')}/",
              f"-sOutputFile={tmp_out}", str(defps), str(src)])
        shutil.move(str(tmp_out), dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Result([dst], f"Архивный формат PDF/A-{level}b")
