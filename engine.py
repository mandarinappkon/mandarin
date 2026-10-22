"""
Mandarin — движок конвертации файлов.

Платформенно-независимая часть: определяет, какие действия доступны для набора
файлов, и выполняет их локально (Pillow, PyMuPDF, ffmpeg, LibreOffice).
Оригиналы никогда не перезаписываются — результат кладётся рядом.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

IMAGE = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff",
         ".gif", ".ico", ".heic", ".heif", ".avif"}
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
         ".aiff", ".aif", ".wma", ".amr"}
VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv",
         ".3gp", ".mts", ".ts"}
PDF = {".pdf"}
DOC = {".docx", ".doc", ".odt", ".rtf", ".pptx", ".ppt", ".odp",
       ".xlsx", ".xls", ".ods"}


class EngineError(Exception):
    pass


@dataclass
class Result:
    outputs: list[Path] = field(default_factory=list)
    message: str = ""
    clipboard: str | None = None


@dataclass
class Action:
    id: str
    label: str      # подпись в колесе (коротко)
    hint: str       # всплывающее пояснение
    run: Callable[..., Result] | None
    ui: str | None = None   # действие открывает окно ("organizer", "viewer", "viewer:sign"), а не выполняется в фоне
    ask: str | None = None  # перед запуском спросить параметры (dialogs.ask); run(paths, **параметры)


# ───────────────────────── общие утилиты ─────────────────────────

def category(p: Path) -> str:
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower()
    for name, group in (("image", IMAGE), ("audio", AUDIO), ("video", VIDEO),
                        ("pdf", PDF), ("doc", DOC)):
        if ext in group:
            return name
    return "other"


def common_category(paths: list[Path]) -> str:
    cats = {category(p) for p in paths}
    return cats.pop() if len(cats) == 1 else "mixed"


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def target(src: Path, ext: str, suffix: str = "") -> Path:
    return unique(src.with_name(src.stem + suffix + ext))


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=_no_window())
    if p.returncode != 0:
        tail = "\n".join((p.stderr or "").strip().splitlines()[-3:])
        raise EngineError(tail or f"Команда завершилась с кодом {p.returncode}")


def _each(paths, fn) -> Result:
    res = Result()
    for p in paths:
        out = fn(p)
        if isinstance(out, (list, tuple)):
            res.outputs.extend(out)
        elif out:
            res.outputs.append(out)
    return res


# ───────────────────────── изображения ─────────────────────────

def _pil():
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image


def heif_supported() -> bool:
    try:
        import pillow_heif  # noqa: F401
        return True
    except ImportError:
        return False


def _has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _flatten(im):
    Image = _pil()
    if _has_alpha(im):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def _open(p: Path):
    from PIL import ImageOps
    im = _pil().open(p)
    if getattr(im, "n_frames", 1) > 1:
        return im
    im.load()
    return ImageOps.exif_transpose(im)


IMAGE_FORMATS = [  # (подпись, расширение, формат Pillow)
    ("JPG", ".jpg", "JPEG"), ("PNG", ".png", "PNG"), ("WEBP", ".webp", "WEBP"),
    ("HEIC", ".heic", "HEIF"), ("GIF", ".gif", "GIF"), ("BMP", ".bmp", "BMP"),
    ("TIFF", ".tiff", "TIFF"), ("ICO", ".ico", "ICO"),
]


def save_image(im, dst: Path, fmt: str, quality: int = 92, keep_meta: bool = True) -> None:
    animated = getattr(im, "n_frames", 1) > 1
    icc = im.info.get("icc_profile") if keep_meta else None
    extra = {"icc_profile": icc} if icc else {}
    if animated and fmt in ("GIF", "WEBP"):
        im.save(dst, fmt, save_all=True, loop=im.info.get("loop", 0),
                duration=im.info.get("duration", 100), quality=quality)
        return
    if animated:
        im.seek(0)
        im = im.convert("RGBA")
    if fmt == "JPEG":
        _flatten(im).save(dst, "JPEG", quality=quality, optimize=True, progressive=True, **extra)
    elif fmt == "WEBP":
        im.convert("RGBA" if _has_alpha(im) else "RGB").save(dst, "WEBP", quality=quality, method=5, **extra)
    elif fmt == "PNG":
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P", "1", "I", "I;16"):
            im = im.convert("RGBA" if _has_alpha(im) else "RGB")
        im.save(dst, "PNG", optimize=True, **extra)
    elif fmt == "BMP":
        _flatten(im).save(dst, "BMP")
    elif fmt == "GIF":
        im.convert("RGBA" if _has_alpha(im) else "RGB").save(dst, "GIF")
    elif fmt == "TIFF":
        (im if im.mode in ("RGB", "RGBA", "L", "CMYK") else im.convert("RGBA")).save(
            dst, "TIFF", compression="tiff_lzw", **extra)
    elif fmt == "ICO":
        im = im.convert("RGBA")
        side = max(im.size)
        sq = _pil().new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= max(side, 16)]
        sq.save(dst, "ICO", sizes=sizes)
    elif fmt == "HEIF":
        if not heif_supported():
            raise EngineError("Для HEIC нужен пакет pillow-heif")
        im.convert("RGBA" if _has_alpha(im) else "RGB").save(dst, "HEIF", quality=quality)
    else:
        raise EngineError(f"Неизвестный формат {fmt}")


def _img_convert(label, ext, fmt):
    def run(paths):
        def one(p):
            if p.suffix.lower() == ext or (ext == ".jpg" and p.suffix.lower() in (".jpeg", ".jfif")):
                return None
            dst = target(p, ext)
            save_image(_open(p), dst, fmt)
            return dst
        return _each(paths, one)
    return Action(f"img_{label.lower()}", label, f"Конвертировать в {label}", run)


def images_to_pdf(paths: list[Path]) -> Result:
    return merge_to_pdf(paths)


def img_compress(paths):
    def one(p):
        im = _open(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.copy()
        im.thumbnail((2560, 2560))
        ext = p.suffix.lower()
        if ext in (".png", ".gif", ".bmp", ".tif", ".tiff", ".ico") and _has_alpha(im):
            dst = target(p, ".webp", " (сжатый)")
            save_image(im, dst, "WEBP", quality=75)
        elif ext == ".webp":
            dst = target(p, ".webp", " (сжатый)")
            save_image(im, dst, "WEBP", quality=72)
        else:
            dst = target(p, ".jpg", " (сжатый)")
            save_image(im, dst, "JPEG", quality=72)
        return dst
    return _shrink_report(paths, _each(paths, one))


def img_half(paths):
    def one(p):
        im = _open(p)
        im = im.resize((max(1, im.width // 2), max(1, im.height // 2)), _pil().LANCZOS)
        fmt, ext = _same_fmt(p)
        dst = target(p, ext, " (50%)")
        save_image(im, dst, fmt)
        return dst
    return _each(paths, one)


def img_strip(paths):
    def one(p):
        im = _open(p)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        clean = im.copy()   # обычный Image без tag_v2/exif; save_image не передаёт exif
        clean.info = {}
        fmt, ext = _same_fmt(p)
        dst = target(p, ext, " (без метаданных)")
        save_image(clean, dst, fmt, quality=95, keep_meta=False)
        return dst
    res = _each(paths, one)
    res.message = "EXIF, GPS и прочие метаданные удалены"
    return res


def img_rotate(paths):
    def one(p):
        im = _open(p).rotate(-90, expand=True)
        fmt, ext = _same_fmt(p)
        dst = target(p, ext, " (повёрнут)")
        save_image(im, dst, fmt, quality=95)
        return dst
    return _each(paths, one)


def img_qr(paths):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise EngineError("Для чтения QR нужен пакет opencv-python-headless")
    found = []
    det = cv2.QRCodeDetector()
    for p in paths:
        arr = np.array(_flatten(_open(p)))[:, :, ::-1].copy()
        ok, texts, *_ = det.detectAndDecodeMulti(arr)
        if ok:
            found.extend(t for t in texts if t)
        else:
            t, *_ = det.detectAndDecode(arr)
            if t:
                found.append(t)
    if not found:
        raise EngineError("QR-код не найден")
    text = "\n".join(found)
    return Result(message=f"QR: {text[:120]}  (скопировано)", clipboard=text)


def _same_fmt(p: Path):
    ext = p.suffix.lower()
    for _, e, fmt in IMAGE_FORMATS:
        if e == ext or (e == ".jpg" and ext in (".jpeg", ".jfif")) or (e == ".tiff" and ext == ".tif") \
                or (e == ".heic" and ext == ".heif"):
            if fmt == "HEIF" and not heif_supported():
                return "JPEG", ".jpg"
            return fmt, ext
    return "PNG", ".png"


# ───────────────────────── аудио / видео (ffmpeg) ─────────────────────────

def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise EngineError("ffmpeg не найден (pip install imageio-ffmpeg)")


AUDIO_FORMATS = {  # подпись: (расширение, аргументы кодека)
    "MP3": (".mp3", ["-c:a", "libmp3lame", "-q:a", "2"]),
    "M4A": (".m4a", ["-c:a", "aac", "-b:a", "192k"]),
    "WAV": (".wav", ["-c:a", "pcm_s16le"]),
    "FLAC": (".flac", ["-c:a", "flac"]),
    "OGG": (".ogg", ["-c:a", "libvorbis", "-q:a", "5"]),
    "OPUS": (".opus", ["-c:a", "libopus", "-b:a", "128k"]),
    "AIFF": (".aiff", ["-c:a", "pcm_s16be"]),
}

VIDEO_FORMATS = {
    "MP4": (".mp4", ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]),
    "MOV": (".mov", ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]),
    "WEBM": (".webm", ["-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-row-mt", "1",
                       "-c:a", "libopus", "-b:a", "128k"]),
    "MKV": (".mkv", ["-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]),
}


def _ff(src: Path, dst: Path, args: list[str], pre: list[str] | None = None) -> Path:
    _run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *(pre or []),
          "-i", str(src), *args, str(dst)])
    return dst


def _audio_convert(label):
    ext, codec = AUDIO_FORMATS[label]

    def run(paths):
        return _each(paths, lambda p: None if p.suffix.lower() == ext
                     else _ff(p, target(p, ext), ["-vn", "-map_metadata", "0", *codec]))
    return Action(f"aud_{label.lower()}", label, f"Конвертировать в {label}", run)


def _video_convert(label):
    ext, codec = VIDEO_FORMATS[label]

    def run(paths):
        return _each(paths, lambda p: None if p.suffix.lower() == ext
                     else _ff(p, target(p, ext), codec))
    return Action(f"vid_{label.lower()}", label, f"Конвертировать в {label}", run)


def video_to_gif(paths):
    flt = "fps=12,scale='min(480,iw)':-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4"
    return _each(paths, lambda p: _ff(p, target(p, ".gif"), ["-filter_complex", flt, "-loop", "0"]))


def video_extract(ext_label):
    ext, codec = AUDIO_FORMATS[ext_label]
    return lambda paths: _each(paths, lambda p: _ff(p, target(p, ext), ["-vn", *codec]))


def video_compress(paths):
    args = ["-vf", "scale=-2:'min(1080,ih)'", "-c:v", "libx264", "-crf", "28", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    return _shrink_report(paths, _each(paths, lambda p: _ff(p, target(p, ".mp4", " (сжатый)"), args)))


def video_mute(paths):
    return _each(paths, lambda p: _ff(p, target(p, p.suffix, " (без звука)"), ["-an", "-c:v", "copy"]))


def video_frame(paths):
    return _each(paths, lambda p: _ff(p, target(p, ".jpg", " (кадр)"), ["-frames:v", "1", "-q:v", "2"],
                                      pre=["-ss", "1"]))


def audio_normalize(paths):
    def one(p):
        ext = p.suffix.lower()
        codec = next((c for e, c in AUDIO_FORMATS.values() if e == ext), AUDIO_FORMATS["M4A"][1])
        if ext not in {e for e, _ in AUDIO_FORMATS.values()}:
            ext = ".m4a"
        return _ff(p, target(p, ext, " (норм)"), ["-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", *codec])
    return _each(paths, one)


def audio_compress(paths):
    return _shrink_report(paths, _each(paths, lambda p: _ff(p, target(p, ".mp3", " (сжатый)"),
                                                           ["-vn", "-c:a", "libmp3lame", "-b:a", "128k"])))


def audio_mono(paths):
    def one(p):
        ext = p.suffix.lower()
        codec = next((c for e, c in AUDIO_FORMATS.values() if e == ext), AUDIO_FORMATS["M4A"][1])
        if ext not in {e for e, _ in AUDIO_FORMATS.values()}:
            ext = ".m4a"
        return _ff(p, target(p, ext, " (моно)"), ["-vn", "-ac", "1", *codec])
    return _each(paths, one)


# ───────────────────────── PDF (PyMuPDF) ─────────────────────────

def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


def pdf_to_images(fmt_ext: str):
    def run(paths):
        fitz = _fitz()
        res = Result()
        for p in paths:
            doc = fitz.open(p)
            folder = unique(p.with_name(p.stem + " — страницы")) if len(doc) > 1 else p.parent
            folder.mkdir(exist_ok=True)
            for i, page in enumerate(doc, 1):
                pix = page.get_pixmap(dpi=150)
                name = f"{p.stem} — {i:0{len(str(len(doc)))}d}{fmt_ext}" if len(doc) > 1 else p.stem + fmt_ext
                out = unique(folder / name)
                if fmt_ext == ".png":
                    pix.save(out)
                else:
                    pix.save(out, jpg_quality=90)
                res.outputs.append(out)
            doc.close()
        return res
    return run


def pdf_to_text(paths):
    fitz = _fitz()

    def one(p):
        with fitz.open(p) as doc:
            text = "\n\n".join(page.get_text() for page in doc)
        dst = target(p, ".txt")
        dst.write_text(text, encoding="utf-8")
        return dst
    return _each(paths, one)


def pdf_split(paths):
    fitz = _fitz()
    res = Result()
    for p in paths:
        src = fitz.open(p)
        folder = unique(p.with_name(p.stem + " — по страницам"))
        folder.mkdir()
        w = len(str(len(src)))
        for i in range(len(src)):
            d = fitz.open()
            d.insert_pdf(src, from_page=i, to_page=i)
            out = folder / f"{p.stem} — {i + 1:0{w}d}.pdf"
            d.save(out, garbage=3, deflate=True)
            d.close()
            res.outputs.append(out)
        src.close()
    return res


def image_pdf_bytes(p: Path) -> bytes:
    """Одна картинка → байты одностраничного PDF (размер страницы по пикселям при 150 dpi)."""
    buf = io.BytesIO()
    _flatten(_open(p)).save(buf, "PDF", resolution=150)
    return buf.getvalue()


def open_as_pdf(p: Path):
    """Открыть PDF или картинку как документ PyMuPDF."""
    fitz = _fitz()
    cat = category(p)
    if cat == "pdf":
        return fitz.open(p)
    if cat == "image":
        return fitz.open("pdf", image_pdf_bytes(p))
    raise EngineError(f"Можно добавлять только PDF и изображения: {p.name}")


def merge_to_pdf(paths: list[Path]) -> Result:
    fitz = _fitz()
    out_doc = fitz.open()
    for p in sorted(paths, key=lambda x: _natural(x.name)):
        with open_as_pdf(p) as d:
            out_doc.insert_pdf(d)
    first = sorted(paths, key=lambda x: _natural(x.name))[0]
    name_src = first if len(paths) == 1 else first.with_name(first.stem + " и ещё " + str(len(paths) - 1))
    dst = target(name_src, ".pdf", "" if len(paths) == 1 and category(first) != "pdf" else " (объединённый)")
    out_doc.save(dst, garbage=3, deflate=True)
    out_doc.close()
    return Result([dst])


def pdf_compress(paths):
    fitz = _fitz()

    def one(p):
        doc = fitz.open(p)
        if hasattr(doc, "rewrite_images"):
            try:
                doc.rewrite_images(dpi_threshold=160, dpi_target=130, quality=70)
            except Exception:
                pass
        dst = target(p, ".pdf", " (сжатый)")
        doc.save(dst, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)
        doc.close()
        return dst
    return _shrink_report(paths, _each(paths, one))


def pdf_strip(paths):
    fitz = _fitz()

    def one(p):
        doc = fitz.open(p)
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        dst = target(p, ".pdf", " (без метаданных)")
        doc.save(dst, garbage=3, deflate=True)
        doc.close()
        return dst
    return _each(paths, one)


def pdf_rotate(paths):
    fitz = _fitz()

    def one(p):
        doc = fitz.open(p)
        for page in doc:
            page.set_rotation((page.rotation + 90) % 360)
        dst = target(p, ".pdf", " (повёрнут)")
        doc.save(dst, garbage=3, deflate=True)
        doc.close()
        return dst
    return _each(paths, one)


def pdf_extract_images(paths):
    fitz = _fitz()
    res = Result()
    for p in paths:
        doc = fitz.open(p)
        folder = unique(p.with_name(p.stem + " — изображения"))
        n = 0
        seen = set()
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                data = doc.extract_image(xref)
                if not data:
                    continue
                folder.mkdir(exist_ok=True)
                n += 1
                out = folder / f"{p.stem} — {n:03d}.{data['ext']}"
                out.write_bytes(data["image"])
                res.outputs.append(out)
        doc.close()
    if not res.outputs:
        raise EngineError("В PDF нет встроенных изображений")
    return res


# ───────────────────────── документы ─────────────────────────
#  Порядок: Microsoft Office (если установлен) → LibreOffice. PDF→DOCX — pdf2docx, без офиса.

WORD_EXT = {".docx", ".doc", ".odt", ".rtf"}
EXCEL_EXT = {".xlsx", ".xls", ".ods"}
PPT_EXT = {".pptx", ".ppt", ".odp"}


def soffice_exe() -> str | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if cand.exists():
                return str(cand)
    return None


_office_cache: dict[str, bool] = {}


def ms_office_has(app: str) -> bool:
    """Установлено ли приложение MS Office (Word/Excel/PowerPoint) — по реестру, без запуска."""
    if os.name != "nt":
        return False
    if app not in _office_cache:
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{app}.Application\\CLSID"))
            _office_cache[app] = True
        except OSError:
            _office_cache[app] = False
    return _office_cache[app]


def _office_app_for(ext: str) -> str | None:
    if ext in WORD_EXT or ext == ".pdf":
        return "Word"
    if ext in EXCEL_EXT:
        return "Excel"
    if ext in PPT_EXT:
        return "PowerPoint"
    return None


# целевой формат → коды SaveAs/Export для приложений Office
_WORD_FMT = {".pdf": 17, ".docx": 16, ".rtf": 6, ".txt": 7, ".odt": 23}
_EXCEL_FMT = {".xlsx": 51, ".csv": 62, ".ods": 60}   # PDF — через ExportAsFixedFormat
_PPT_FMT = {".pdf": 32, ".pptx": 24, ".odp": 35}


def office_can(src_ext: str, out_ext: str) -> bool:
    app = _office_app_for(src_ext)
    if not app or not ms_office_has(app):
        return False
    table = {"Word": _WORD_FMT, "Excel": {**_EXCEL_FMT, ".pdf": 0}, "PowerPoint": _PPT_FMT}[app]
    return out_ext in table


def _office_convert(src: Path, dst: Path) -> None:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app_name = _office_app_for(src.suffix.lower())
    out_ext = dst.suffix.lower()
    app = win32com.client.DispatchEx(f"{app_name}.Application")
    try:
        if app_name == "Word":
            app.Visible = False
            app.DisplayAlerts = 0
            doc = app.Documents.Open(str(src), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(dst), FileFormat=_WORD_FMT[out_ext])
            finally:
                doc.Close(False)
        elif app_name == "Excel":
            app.Visible = False
            app.DisplayAlerts = False
            wb = app.Workbooks.Open(str(src), ReadOnly=True)
            try:
                if out_ext == ".pdf":
                    wb.ExportAsFixedFormat(0, str(dst))
                else:
                    wb.SaveAs(str(dst), FileFormat=_EXCEL_FMT[out_ext])
            finally:
                wb.Close(False)
        else:
            pres = app.Presentations.Open(str(src), ReadOnly=True, WithWindow=False)
            try:
                pres.SaveAs(str(dst), _PPT_FMT[out_ext])
            finally:
                pres.Close()
    finally:
        try:
            app.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()
    if not dst.exists():
        raise EngineError(f"{app_name} не создал файл")


_LO_FILTERS = {".docx": "docx:MS Word 2007 XML", ".txt": "txt:Text (encoded):UTF8"}


def _lo_convert(src: Path, dst: Path) -> None:
    exe = soffice_exe()
    out_ext = dst.suffix.lower()
    tmp = unique(src.with_name(".mandarin-tmp"))
    tmp.mkdir()
    try:
        infilter = ["--infilter=writer_pdf_import"] if src.suffix.lower() == ".pdf" else []
        fmt = _LO_FILTERS.get(out_ext, out_ext.lstrip("."))
        _run([exe, "--headless", *infilter, "--convert-to", fmt, "--outdir", str(tmp), str(src)])
        produced = next(tmp.iterdir(), None)
        if not produced:
            raise EngineError("LibreOffice не создал файл")
        shutil.move(str(produced), dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def doc_backend(src_ext: str, out_ext: str) -> str | None:
    if office_can(src_ext, out_ext):
        return "office"
    if soffice_exe():
        return "libreoffice"
    return None


def _doc_convert(out_ext: str):
    def run(paths):
        def one(p):
            backend = doc_backend(p.suffix.lower(), out_ext)
            if not backend:
                raise EngineError("Нужен Microsoft Office или бесплатный LibreOffice (libreoffice.org)")
            dst = target(p, out_ext)
            (_office_convert if backend == "office" else _lo_convert)(p, dst)
            return dst
        return _each(paths, one)
    return run


def pdf_to_docx(paths):
    try:
        from pdf2docx import Converter
    except ImportError:
        return _doc_convert(".docx")(paths)
    import logging
    logging.getLogger().setLevel(logging.WARNING)

    def one(p):
        dst = target(p, ".docx")
        cv = Converter(str(p))
        try:
            cv.convert(str(dst))
        finally:
            cv.close()
        return dst
    return _each(paths, one)


def documents_supported(paths: list[Path]) -> bool:
    return all(doc_backend(p.suffix.lower(), ".pdf") for p in paths)


# ───────────────────────── любые файлы ─────────────────────────

def make_zip(paths):
    if len(paths) == 1:
        p = paths[0]
        dst = unique(p.parent / (p.name + ".zip") if p.is_dir() else p.with_suffix(".zip"))
    else:
        dst = unique(paths[0].parent / "Архив.zip")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in paths:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        z.write(f, Path(p.name) / f.relative_to(p))
            else:
                z.write(p, p.name)
    return Result([dst])


def sha256(paths):
    lines = []
    for p in paths:
        if p.is_dir():
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        lines.append(h.hexdigest() if len(paths) == 1 else f"{h.hexdigest()}  {p.name}")
    text = "\n".join(lines)
    return Result(message=f"SHA-256 скопирован: {text[:20]}…", clipboard=text)


def copy_paths(paths):
    text = "\n".join(str(p) for p in paths)
    return Result(message="Путь скопирован", clipboard=text)


# ───────────────────────── каталог действий ─────────────────────────

def actions_for(paths: list[Path], mode: str = "convert") -> list[Action]:
    """mode: 'convert' (Shift) или 'tools' (Shift+Alt)."""
    cat = common_category(paths)
    A = Action
    acts: list[Action] = []

    if mode == "convert":
        if cat == "image":
            skip = {_same_fmt(paths[0])[1]} if len(paths) == 1 else set()
            for label, ext, fmt in IMAGE_FORMATS:
                if fmt == "HEIF" and not heif_supported():
                    continue
                if ext in skip:
                    continue
                acts.append(_img_convert(label, ext, fmt))
            acts.append(A("img_pdf", "PDF", "Собрать в PDF", images_to_pdf))
        elif cat == "audio":
            exts = {p.suffix.lower() for p in paths}
            acts = [_audio_convert(l) for l, (e, _) in AUDIO_FORMATS.items() if not (len(exts) == 1 and e in exts)]
        elif cat == "video":
            exts = {p.suffix.lower() for p in paths}
            acts = [_video_convert(l) for l, (e, _) in VIDEO_FORMATS.items() if not (len(exts) == 1 and e in exts)]
            acts += [A("vid_gif", "GIF", "Анимированный GIF", video_to_gif),
                     A("vid_mp3", "MP3", "Извлечь звук в MP3", video_extract("MP3")),
                     A("vid_m4a", "M4A", "Извлечь звук в M4A", video_extract("M4A"))]
        elif cat == "pdf":
            import pdftools as P
            acts = [A("pdf_docx", "DOCX", "В Word (редактируемый)", pdf_to_docx),
                    A("pdf_xlsx", "XLSX", "Таблицы в Excel", P.export_xlsx),
                    A("pdf_pptx", "PPTX", "Страницы в PowerPoint", P.export_pptx),
                    A("pdf_png", "PNG", "Страницы в PNG", pdf_to_images(".png")),
                    A("pdf_jpg", "JPG", "Страницы в JPG", pdf_to_images(".jpg")),
                    A("pdf_txt", "TXT", "Извлечь текст", pdf_to_text)]
            if P.pdfa_available():
                acts.append(A("pdf_pdfa", "PDF/A", "Архивный PDF/A-2b", per_file(P.to_pdfa)))
        elif cat == "doc":
            exts = {p.suffix.lower() for p in paths}
            ext = next(iter(exts)) if len(exts) == 1 else None
            if ext in WORD_EXT:
                cands = [(".pdf", "PDF"), (".docx", "DOCX"), (".rtf", "RTF"), (".odt", "ODT"), (".txt", "TXT")]
            elif ext in EXCEL_EXT:
                cands = [(".pdf", "PDF"), (".xlsx", "XLSX"), (".csv", "CSV"), (".ods", "ODS")]
            elif ext in PPT_EXT:
                cands = [(".pdf", "PDF"), (".pptx", "PPTX"), (".odp", "ODP")]
            else:
                cands = [(".pdf", "PDF")]
            for out_ext, label in cands:
                if out_ext == ext:
                    continue
                if all(doc_backend(p.suffix.lower(), out_ext) for p in paths):
                    acts.append(A(f"doc_{label.lower()}", label, f"Конвертировать в {label}", _doc_convert(out_ext)))
        elif cat == "mixed" and all(category(p) in ("image", "pdf") for p in paths):
            acts = [A("mix_pdf", "PDF", "Собрать всё в один PDF", merge_to_pdf),
                    A("organize", "Порядок", "Открыть редактор страниц", None, ui="organizer")]
    else:  # tools
        if cat == "image":
            acts = [A("img_compress", "Сжать", "Уменьшить вес для отправки", img_compress),
                    A("img_half", "50%", "Уменьшить вдвое", img_half),
                    A("img_rotate", "Поворот", "Повернуть по часовой", img_rotate),
                    A("img_strip", "Без EXIF", "Удалить метаданные и геометки", img_strip),
                    A("img_qr", "QR", "Прочитать QR-код", img_qr)]
        elif cat == "audio":
            acts = [A("aud_compress", "Сжать", "MP3 128 кбит/с", audio_compress),
                    A("aud_norm", "Громк.", "Нормализовать громкость", audio_normalize),
                    A("aud_mono", "Моно", "Свести в моно", audio_mono)]
        elif cat == "video":
            acts = [A("vid_compress", "Сжать", "MP4 до 1080p, меньше вес", video_compress),
                    A("vid_mute", "Без звука", "Убрать звуковую дорожку", video_mute),
                    A("vid_frame", "Кадр", "Сохранить кадр в JPG", video_frame)]
        elif cat == "pdf":
            import pdftools as P
            acts = [
                # первая страница колеса — самое частое (как в Acrobat)
                A("pdf_open", "Открыть", "PDF-редактор: комментарии, подпись, формы", None, ui="viewer"),
                A("pdf_sign", "Подпись", "Поставить подпись мышью", None, ui="viewer:sign"),
                A("pdf_protect", "Пароль", "Защитить паролем (AES-256)", per_file(P.protect), ask="protect"),
                A("pdf_ocr", "OCR", "Распознать скан: поиск и копирование текста", per_file(P.ocr_file)),
                A("pdf_compress", "Сжать", "Уменьшить вес PDF", pdf_compress),
                A("organize", "Порядок", "Редактор страниц: порядок, поворот, удаление", None, ui="organizer"),
                A("pdf_redact", "Зачернить", "Скрыть e-mail, телефоны, паспорта навсегда", per_file(P.redact_file),
                  ask="redact"),
                A("pdf_watermark", "Знак", "Водяной знак на всех страницах", per_file(P.watermark_file),
                  ask="watermark"),
                # дальше — по «Ещё»
                A("pdf_numbers", "Номера", "Номера страниц", per_file(P.numbers_file), ask="numbers"),
                A("pdf_headers", "Колонтит.", "Верхний и нижний колонтитулы", per_file(P.header_footer_file),
                  ask="header_footer"),
                A("pdf_bates", "Bates", "Сквозная нумерация Бейтса", P.bates_files, ask="bates"),
                A("pdf_crop", "Обрезать", "Обрезать поля страниц", per_file(P.crop_file), ask="crop"),
                A("pdf_compare", "Сравнить", "Сравнить две версии документа",
                  lambda paths, a, b: P.compare_files(a, b), ask="compare"),
                A("pdf_cert", "Сертификат", "Подписать цифровым сертификатом", per_file(P.sign_with_certificate),
                  ask="sign_cert"),
                A("pdf_verify", "Проверка", "Проверить цифровые подписи", per_file(P.verify_file)),
                A("pdf_unlock", "Без пароля", "Снять пароль", per_file(P.unlock), ask="unlock"),
                A("pdf_flatten", "Сгладить", "Вшить формы и комментарии", per_file(P.flatten_file)),
                A("pdf_split", "Разбить", "По одной странице", pdf_split),
                A("pdf_rotate", "Поворот", "Повернуть страницы", pdf_rotate),
                A("pdf_strip", "Без EXIF", "Удалить метаданные", pdf_strip),
                A("pdf_imgs", "Картинки", "Извлечь изображения", pdf_extract_images),
            ]
            if len(paths) > 1:
                acts.insert(5, A("pdf_merge", "Объед.", "Объединить в один PDF", merge_to_pdf))
        acts += [A("any_zip", "ZIP", "Упаковать в архив", make_zip),
                 A("any_sha", "SHA-256", "Хеш в буфер обмена", sha256),
                 A("any_path", "Путь", "Скопировать путь", copy_paths)]
    return acts


def per_file(fn):
    """fn(path, **kw) → Result для одного файла  ⇒  run(paths, **kw) для нескольких."""
    def run(paths, **kw):
        res = Result()
        for p in paths:
            r = fn(p, **kw)
            res.outputs.extend(r.outputs)
            res.message = r.message or res.message
            if r.clipboard is not None:
                res.clipboard = r.clipboard
        return res
    run.__name__ = getattr(fn, "__name__", "run")
    return run


WHEEL_PAGE = 9          # плиток на одной странице колеса (последняя — «Ещё», если действий больше)


def paginate(actions: list[Action], page: int) -> tuple[list[Action], int]:
    """Страница колеса: до 9 плиток; если действий больше — 8 + «Ещё». Возвращает (плитки, всего страниц)."""
    if len(actions) <= WHEEL_PAGE:
        return actions, 1
    per = WHEEL_PAGE - 1
    pages = (len(actions) + per - 1) // per
    page %= pages
    more = Action("_more", "Ещё", f"Ещё действия · {page + 1}/{pages}", None)
    return actions[page * per:(page + 1) * per] + [more], pages


def _shrink_report(paths: list[Path], res: Result) -> Result:
    """Удаляет результаты сжатия, которые не стали меньше исходника."""
    kept, before, after = [], 0, 0
    for src, out in zip(paths, res.outputs):
        a, b = src.stat().st_size, out.stat().st_size
        if b >= a:
            out.unlink()
        else:
            kept.append(out)
            before, after = before + a, after + b
    res.outputs = kept
    if not kept:
        raise EngineError("Файл уже компактный — сжимать нечего")
    res.message = f"Размер: {_human(before)} → {_human(after)} (−{100 - after * 100 // before}%)"
    return res


def _human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _natural(s: str):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
