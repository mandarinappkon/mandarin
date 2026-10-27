"""
Mandarin — аналог Tangerine для Windows.

Жест: начните перетаскивать файл в Проводнике и зажмите Shift — у курсора появится
колесо форматов. Отпустите файл на нужном секторе — копия в новом формате появится
рядом с оригиналом. Shift+Alt — колесо инструментов (сжать, повернуть, без EXIF, QR…).

Запуск:
    Mandarin.exe                 — фоновый режим (значок в трее)
    Mandarin.exe --menu ФАЙЛЫ…   — колесо по клику (так работает «Отправить → Mandarin»)
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import engine
import license as lic
import wheel_render

APP = "Mandarin"
IS_WIN = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
ICON = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "mandarin.ico"

# ───────────────────────── настройки ─────────────────────────

def settings_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULTS = {"enabled": True, "everywhere": False, "first_run_done": False, "tools_key": "ctrl"}


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads((settings_dir() / "settings.json").read_text("utf-8"))}
    except Exception:
        return dict(DEFAULTS)


def save_settings(s: dict) -> None:
    (settings_dir() / "settings.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")


_trace_lock = threading.Lock()


def trace(text: str) -> None:
    """Короткий журнал шагов жеста — по нему видно, на каком шаге случился вылет."""
    try:
        with _trace_lock:
            path = settings_dir() / "debug.log"
            if path.exists() and path.stat().st_size > 300_000:
                path.replace(path.with_suffix(".old.log"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + text + "\n")
    except Exception:
        pass


def enable_crash_log() -> None:
    """Нативные падения (Tk, OLE, COM) не дают Python-ошибки — faulthandler пишет их стек в crash.log."""
    try:
        import faulthandler
        f = open(settings_dir() / "crash.log", "a", encoding="utf-8")
        f.write(time.strftime("\n=== запуск %Y-%m-%d %H:%M:%S ===\n"))
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        globals()["_crash_file"] = f
    except Exception:
        pass


def log_error(text: str) -> None:
    try:
        with open(settings_dir() / "errors.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except Exception:
        pass


# ───────────────────────── Win32 ─────────────────────────
if IS_WIN:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

VK_LBUTTON, VK_RBUTTON, VK_SHIFT, VK_CONTROL, VK_MENU, VK_ESCAPE = 0x01, 0x02, 0x10, 0x11, 0x12, 0x1B

# Окна, где жест включён по умолчанию: Проводник и рабочий стол
EXPLORER_ROOTS = {"CabinetWClass", "ExploreWClass", "Progman", "WorkerW"}
EXPLORER_VIEWS = {"DirectUIHWND", "SysListView32", "SysTreeView32", "SHELLDLL_DefView"}


def set_dpi_aware() -> None:
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def primary_button() -> int:
    return VK_RBUTTON if user32.GetSystemMetrics(23) else VK_LBUTTON  # SM_SWAPBUTTON


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_info(x: int, y: int) -> tuple[int, str, str]:
    """(hwnd корня, класс корня, класс окна под курсором)."""
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not h:
        return 0, "", ""
    root = user32.GetAncestor(h, 2) or h  # GA_ROOT
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    child = buf.value
    user32.GetClassNameW(root, buf, 256)
    return int(root), buf.value, child


def work_area() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def set_clipboard(text: str) -> None:
    """Через Win32, чтобы текст оставался в буфере и после выхода процесса."""
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, size)
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)  # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


def explorer_selection(root_hwnd: int) -> list[Path]:
    """Выделенные файлы в окне Проводника (если установлен pywin32)."""
    try:
        import win32com.client
    except ImportError:
        return []
    try:
        for w in win32com.client.Dispatch("Shell.Application").Windows():
            try:
                if int(w.HWND) != root_hwnd:
                    continue
                items = w.Document.SelectedItems()
                paths = [Path(items.Item(i).Path) for i in range(items.Count)]
                if paths:
                    return paths
            except Exception:
                continue
    except Exception:
        pass
    return []


def single_instance() -> bool:
    if not IS_WIN:
        return True
    kernel32.CreateMutexW(None, False, "Local\\MandarinFileWheel")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def reveal(path: Path) -> None:
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(path)])


# ───────────────────────── интеграция с системой ─────────────────────────

def self_command() -> tuple[str, list[str]]:
    if FROZEN:
        return sys.executable, []
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else sys.executable), [str(Path(__file__).resolve())]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe, args = self_command()
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline([exe, *args, "--background"]))
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


def sendto_link() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "SendTo" / f"{APP}.lnk"


def set_sendto(on: bool) -> None:
    link = sendto_link()
    if not on:
        link.unlink(missing_ok=True)
        return
    exe, args = self_command()
    icon = Path(exe) if FROZEN else ICON
    q = lambda s: str(s).replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(link)}');"
          f"$s.TargetPath='{q(exe)}';"
          f"$s.Arguments='{q(subprocess.list2cmdline([*args, '--menu']))}';"
          f"$s.Description='Конвертировать с помощью Mandarin';"
          + (f"$s.IconLocation='{q(icon)}';" if icon.exists() else "")
          + "$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=0x08000000, check=False)


# ───────────────────────── приложение ─────────────────────────

class MandarinApp:

    def __init__(self, launch: str = "background", files: list[Path] | None = None, show_studio: bool = False):
        """launch: background (трей + жест) · studio (только окно) · menu (колесо по клику) · organize."""
        import tkinter as tk
        from tkinterdnd2 import TkinterDnD, DND_FILES
        self.tk = tk
        self.root = TkinterDnD.Tk()
        self.root.withdraw()
        self.root.title(APP)
        self.root.report_callback_exception = self._on_tk_error
        self.launch = launch
        self.studio = None
        self.organizers: list = []
        click_files = files if launch == "menu" else None
        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.click_mode = click_files is not None

        # состояние жеста
        self.state = "idle"
        self.press = None          # (x, y, root_hwnd, root_class, child_class)
        self.mode = "convert"
        self.files: list[Path] = []
        self.actions: list[engine.Action] = []      # плитки текущей страницы колеса
        self.all_actions: list[engine.Action] = []  # все действия для файлов
        self.wheel_page = 0
        self.page_count = 1
        self._dwell_job = None
        self.hover = -1
        self.hide_job = None
        self.vk_btn = primary_button()
        self._prev_shift = self._prev_tools = False
        self._job = None
        self._card_pos = None
        self.card_anchor = None

        self._build_wheel(DND_FILES)
        self.toast_win = None

        if launch == "menu":
            self.files = click_files
            x, y = cursor_pos()
            self.show_wheel(x, y, "convert")
            self.root.after(30, self._click_poll)
        elif launch == "studio":
            self.open_studio()
        elif launch == "organize":
            self.open_organizer(files or [])
        elif launch == "pdf":
            self.open_viewer(files or [])
        else:
            self._start_tray()
            self.root.after(15, self._poll)
            if files:
                self.open_viewer(files) if engine.category(files[0]) == "pdf" else self.open_studio()
            elif show_studio:
                self.open_studio()
            else:
                tools = "Ctrl" if self.settings.get("tools_key", "ctrl") == "ctrl" else "Alt"
                self.toast("Mandarin работает", f"Начните перетаскивать файл и нажмите Shift — форматы, {tools} — "
                           "инструменты. Окно программы — по щелчку на значке в трее.")
        self.root.after(50, self._drain)

    # ── окна ──
    def open_studio(self):
        import studio
        if self.studio is not None:
            try:
                self.studio.show()
                return
            except self.tk.TclError:
                self.studio = None
        self.studio = studio.Studio(self.root, self.open_organizer,
                                    set_clipboard=set_clipboard if IS_WIN else None, log_error=log_error,
                                    open_viewer=self.open_viewer, ask=self._ask_params)
        self.studio.win.protocol("WM_DELETE_WINDOW", self._close_studio)
        self.studio.show()

    def _close_studio(self):
        if self.launch == "background":
            self.studio.win.withdraw()          # программа остаётся в трее
        else:
            self.studio.win.destroy()
            self.studio = None
            self._maybe_exit()

    def open_organizer(self, files: list[Path]):
        import organizer
        org = organizer.PdfOrganizer(self.root, files, on_done=lambda t, b, p: self.toast(t, b, on_click=lambda: reveal(p)))
        self.organizers.append(org)
        org.win.bind("<Destroy>", lambda e, o=org: e.widget is o.win and self._organizer_closed(o), add="+")

    def open_viewer(self, files: list[Path], tool: str | None = None):
        import license_ui
        import viewer
        if not license_ui.require_pro(self.root, engine.Action("pdf_open", "Открыть", "PDF-редактор", None,
                                                               ui="viewer")):
            self.root.after(100, self._maybe_exit)
            return None
        path = next((f for f in files if engine.category(f) in ("pdf", "image")), None)
        v = viewer.PdfViewer(self.root, path, on_done=lambda t, b, p: self.toast(t, b, on_click=(lambda: reveal(p))
                                                                                    if p else None),
                             on_open_organizer=self.open_organizer)
        self.organizers.append(v)
        v.win.bind("<Destroy>", lambda e, o=v: e.widget is o.win and self._organizer_closed(o), add="+")
        if tool:
            v.win.after(400, lambda: v.set_tool(tool))
        return v

    def _ask_params(self, action, files):
        """Диалог параметров для действия (пароль, водяной знак…). None — отменено."""
        if not action.ask:
            return {}
        import dialogs
        return dialogs.ask(action.ask, self.root, files)

    def _organizer_closed(self, org):
        if org in self.organizers:
            self.organizers.remove(org)
        self.root.after(100, self._maybe_exit)

    def _maybe_exit(self):
        """В режимах без трея закрываемся, когда не осталось открытых окон."""
        if self.launch == "background":
            return
        if self.organizers or self.studio is not None:
            return
        if self.launch == "menu" and self.wheel.winfo_viewable():
            return
        self.root.after(200, self.root.destroy)

    def _on_tk_error(self, exc, val, tb):
        log_error("".join(traceback.format_exception(exc, val, tb)))
        try:
            self.toast("Ошибка", f"{exc.__name__}: {val}", error=True)
        except Exception:
            pass

    # ── окно-колесо (в стиле Tangerine) ──
    def _build_wheel(self, DND_FILES):
        tk = self.tk
        self.k = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.g = wheel_render.Geometry(self.k)
        g = self.g
        self.key_hex = "#%02x%02x%02x" % wheel_render.KEY
        w = tk.Toplevel(self.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=self.key_hex)
        try:
            w.attributes("-transparentcolor", self.key_hex)
        except tk.TclError:
            pass
        c = tk.Canvas(w, width=g.W, height=g.H, bg=self.key_hex, highlightthickness=0, bd=0)
        c.pack()
        self.wheel_item = c.create_image(0, 0, anchor="nw")
        c.drop_target_register(DND_FILES)
        c.dnd_bind("<<DropEnter>>", self._safe_dnd(self._on_drop_enter, "copy"))
        c.dnd_bind("<<DropPosition>>", self._safe_dnd(self._on_drop_position, "copy"))
        c.dnd_bind("<<DropLeave>>", self._safe_dnd(lambda e: "copy", "copy"))
        c.dnd_bind("<<Drop>>", self._safe_dnd(self._on_drop, "copy"))
        c.bind("<Motion>", lambda e: self._set_hover(self._index_at(e.x_root, e.y_root)))
        c.bind("<Button-1>", self._on_click)
        c.bind("<Button-3>", lambda e: self._cancel_pick())
        for seq, fn in (("<Escape>", lambda e: self._cancel_pick()),
                        ("<Left>", lambda e: self._step_hover(-1)), ("<Up>", lambda e: self._step_hover(-1)),
                        ("<Right>", lambda e: self._step_hover(1)), ("<Down>", lambda e: self._step_hover(1)),
                        ("<Tab>", lambda e: self._toggle_mode()), ("<Return>", lambda e: self._apply_hover())):
            w.bind(seq, fn)
        self.wheel, self.canvas = w, c
        self._wheel_cache: dict = {}

    def _center_text(self) -> str:
        if 0 <= self.hover < len(self.actions):
            return self.actions[self.hover].label
        if not self.files:
            return "Формат" if self.mode == "convert" else "Инструменты"
        if len(self.files) > 1:
            return f"{len(self.files)} файла" if len(self.files) < 5 else f"{len(self.files)} файлов"
        try:
            f = self.files[0]
            return "Папка" if f.is_dir() else engine._human(f.stat().st_size)
        except OSError:
            return self.files[0].suffix.upper().lstrip(".")

    def _caption(self) -> str:
        if 0 <= self.hover < len(self.actions):
            a = self.actions[self.hover]
            if a.id == "_more" and not (self.click_mode or self.state == "picking"):
                return "Задержите здесь — следующие действия"
            return a.hint + (" · нужна Pro" if lic.needs_pro(a) else "")
        if not self.actions:
            return "Отпустите файл здесь"
        tools = "Ctrl" if self.settings.get("tools_key", "ctrl") == "ctrl" else "Alt"
        page = f" · стр. {self.wheel_page + 1}/{self.page_count}" if self.page_count > 1 else ""
        if self.click_mode or self.state == "picking":
            return "Клик — выбрать · Tab — " + ("инструменты" if self.mode == "convert" else "форматы") + page
        return (f"{tools} — инструменты" if self.mode == "convert" else "Shift — форматы") + page

    def _draw(self):
        from PIL import ImageTk
        labels = tuple(a.label for a in self.actions)
        icons = tuple(wheel_render.ICON_FOR.get(a.id) for a in self.actions) if self.mode == "tools" else None
        key = (labels, icons, self.hover, self._center_text(), self._caption())
        photo = self._wheel_cache.get(key)
        if photo is None:
            im = wheel_render.render_mandarin(list(labels), self.hover, key[3], key[4],
                                              icons=list(icons) if icons else None, scale=self.k)
            photo = ImageTk.PhotoImage(im)
            if len(self._wheel_cache) > 60:
                self._wheel_cache.clear()
            self._wheel_cache[key] = photo
        self.canvas.itemconfigure(self.wheel_item, image=photo)

    def _local(self, x, y):
        return x - self.wheel.winfo_rootx(), y - self.wheel.winfo_rooty()

    def _index_at(self, x, y) -> int:
        lx, ly = self._local(x, y)
        return self.g.index_at(lx, ly, len(self.actions))

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self._draw()
            # «Ещё»: задержали курсор (или файл) на плитке — листаем колесо
            if self._dwell_job:
                self.root.after_cancel(self._dwell_job)
                self._dwell_job = None
            if 0 <= i < len(self.actions) and self.actions[i].id == "_more":
                self._dwell_job = self.root.after(550, self._dwell_fire)

    def _dwell_fire(self):
        self._dwell_job = None
        if 0 <= self.hover < len(self.actions) and self.actions[self.hover].id == "_more":
            self._next_page()

    def _next_page(self):
        self.wheel_page = (self.wheel_page + 1) % max(1, self.page_count)
        self._paginate()
        trace(f"Колесо: страница {self.wheel_page + 1}/{self.page_count}")

    def _step_hover(self, d):
        if self.actions:
            self._set_hover((max(self.hover, 0 if d > 0 else 1) + d) % len(self.actions) if self.hover >= 0
                            else (0 if d > 0 else len(self.actions) - 1))

    def _apply_hover(self):
        if (self.click_mode or self.state == "picking") and 0 <= self.hover < len(self.actions):
            self._pick(self.hover)

    def _toggle_mode(self):
        if self.click_mode or self.state == "picking":
            self.mode = "tools" if self.mode == "convert" else "convert"
            self._load_actions()

    def _load_actions(self):
        self.all_actions = engine.actions_for(self.files, self.mode) if self.files else []
        self.wheel_page = 0
        self._paginate()

    def _paginate(self):
        self.actions, self.page_count = engine.paginate(self.all_actions, self.wheel_page)
        self.hover = -1
        self._draw()

    def show_wheel(self, x, y, mode, root_hwnd=0):
        self.mode = mode
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        if not self.click_mode:
            self.files = []
        self._load_actions()
        g = self.g
        left, top, right, bottom = work_area() if IS_WIN else (0, 0, 10 ** 5, 10 ** 5)
        gx = min(max(x - g.CX, left), right - g.W)
        gy = min(max(y - g.CY, top), bottom - g.H)
        self.wheel.geometry(f"{g.W}x{g.H}+{int(gx)}+{int(gy)}")
        self.wheel.deiconify()
        self.wheel.lift()
        if self.click_mode:
            self.wheel.focus_force()
        # Список файлов приходит от tkdnd при наведении (<<DropEnter>>). Обращаться к Проводнику
        # через COM во время перетаскивания нельзя: встречные вызовы роняли программу.

    def hide_wheel(self, close=False):
        self.hide_job = None
        self.wheel.withdraw()
        self.hover = -1
        if close and self.click_mode:
            self.root.after(10, self._maybe_exit)

    def _hide_if_idle(self):
        self.hide_job = None
        if self.state != "picking":
            self.hide_wheel()

    def _cancel_pick(self):
        self.state = "idle"
        self.hide_wheel(close=True)

    # ── события перетаскивания ──
    def _safe_dnd(self, fn, default):
        """Обработчики OLE вызываются Проводником; исключение не должно уйти обратно в tkdnd."""
        def wrapper(event):
            try:
                res = fn(event)
                return res if res in ("copy", "refuse_drop", "move", "link") else default
            except Exception:
                log_error("DnD: " + traceback.format_exc())
                return default
        return wrapper

    def _files_from(self, event) -> list[Path]:
        try:
            return [Path(p) for p in self.root.tk.splitlist(event.data) if p]
        except Exception:
            return []

    def _on_drop_enter(self, event):
        files = self._files_from(event)
        trace(f"DropEnter: {len(files)} файл(ов) {files[0].suffix if files else ''}")
        if files and files != self.files:
            self.files = files
            self._load_actions()
        return "copy"

    def _on_drop_position(self, event):
        self._set_hover(self._index_at(event.x_root, event.y_root))
        return "copy" if self.hover >= 0 or not self.actions else "refuse_drop"

    def _on_drop(self, event):
        # Здесь только запоминаем, что выбрано, и сразу возвращаем ответ Проводнику.
        # Прятать окно-цель и создавать окна внутри Drop нельзя — это роняло tkdnd/OLE.
        files = self._files_from(event)
        idx = self._index_at(event.x_root, event.y_root)
        shown = [a.label for a in self.actions]
        mode = self.mode
        self.card_anchor = (event.x_root, event.y_root)
        trace(f"Drop: файлов={len(files)} плитка={idx} режим={mode}")
        if not files:
            return "refuse_drop"
        self.root.after(30, lambda: self._after_drop(files, idx, shown, mode))
        return "copy"

    def _after_drop(self, files, idx, shown, mode):
        if self.state == "armed":
            self.state = "idle"
        all_real = engine.actions_for(files, mode)
        if not all_real:
            self.hide_wheel()
            self.toast(*no_actions_message(files, mode), error=True)
            return
        real, _ = engine.paginate(all_real, self.wheel_page)
        if idx >= 0 and [a.label for a in real] == shown:
            act = real[idx]
            if act.id == "_more":
                # отпустили на «Ещё» — показываем следующую страницу и ждём клика
                self._reopen_for_click(files, mode, page=self.wheel_page + 1)
                return
            self.hide_wheel()
            trace(f"Запуск: {act.id}")
            self.run_action(act, files)
        else:
            # колесо не знало файлы заранее — оставляем его открытым для выбора кликом
            self._reopen_for_click(files, mode)

    def _reopen_for_click(self, files, mode, page=0):
        self.state = "picking"
        self.files = files
        self.mode = mode
        self.all_actions = engine.actions_for(files, mode)
        self.wheel_page = page
        self._paginate()
        self.wheel.deiconify()
        self.wheel.lift()
        self.wheel.focus_force()

    def _pick(self, idx):
        act, files = self.actions[idx], list(self.files)
        if act.id == "_more":
            self._next_page()
            return
        self.card_anchor = cursor_pos() if IS_WIN else None
        self.hide_wheel()
        self.state = "idle"
        self.run_action(act, files, exit_after=self.click_mode)

    def _on_click(self, event):
        if not (self.click_mode or self.state == "picking"):
            return
        idx = self._index_at(event.x_root, event.y_root)
        if idx >= 0:
            self._pick(idx)
        elif self.g.in_center(*self._local(event.x_root, event.y_root)):
            self._toggle_mode()

    # ── опрос мыши и клавиатуры (без глобальных хуков) ──
    def _poll(self):
        try:
            self._poll_once()
        except Exception:
            log_error(traceback.format_exc())
        self.root.after(15, self._poll)

    def _gesture_allowed(self) -> bool:
        if not self.settings["enabled"] or not self.press:
            return False
        _, _, root_cls, child_cls = self.press
        if root_cls == "TkTopLevel" or child_cls.startswith("Tk"):
            return False
        if self.settings["everywhere"]:
            return True
        return root_cls in EXPLORER_ROOTS and child_cls in EXPLORER_VIEWS

    def _poll_once(self):
        down = key_down(self.vk_btn)
        x, y = cursor_pos()
        shift = key_down(VK_SHIFT)
        tools_vk = VK_CONTROL if self.settings.get("tools_key", "ctrl") == "ctrl" else VK_MENU
        tools = key_down(tools_vk)
        # «нажатие» = переход отпущено → нажато (как в Tangerine: нажали клавишу — колесо открыто до отпускания)
        shift_tap = shift and not self._prev_shift
        tools_tap = tools and not self._prev_tools
        self._prev_shift, self._prev_tools = shift, tools

        if self.state == "picking":
            if key_down(VK_ESCAPE):
                self._cancel_pick()
            elif down:
                lx, ly = self._local(x, y)
                if not (0 <= lx < self.g.W and 0 <= ly < self.g.H):
                    self._cancel_pick()
            return

        if self.state == "idle":
            if down:
                hwnd, rc, cc = window_info(x, y)
                self.press = (x, y, rc, cc)
                self.press_hwnd = hwnd
                self.state = "pressing"
        elif self.state == "pressing":
            if not down:
                self.state = "idle"
            elif math.hypot(x - self.press[0], y - self.press[1]) > 12:
                self.state = "dragging"
        if self.state == "dragging":
            if not down:
                self.state = "idle"
            elif (shift or tools) and self._gesture_allowed():
                self.state = "armed"
                mode = "tools" if tools and not shift_tap else "convert"
                trace(f"Колесо открыто: {mode}, окно {self.press[2]}/{self.press[3]}")
                self.show_wheel(x, y, mode, self.press_hwnd)
        elif self.state == "armed":
            if not down:
                self.state = "idle"
                trace("Кнопка мыши отпущена")
                # даём OLE доставить Drop до скрытия
                self.hide_job = self.root.after(500, self._hide_if_idle)
            elif shift_tap and self.mode != "convert":
                self.mode = "convert"; self._load_actions()
            elif tools_tap and self.mode != "tools":
                self.mode = "tools"; self._load_actions()
            elif key_down(VK_ESCAPE):
                self.state = "idle"
                self.hide_wheel()

    def _click_poll(self):
        # режим «Отправить»: закрыть при клике вне колеса
        try:
            x, y = cursor_pos()
            lx, ly = self._local(x, y)
            inside = 0 <= lx < self.g.W and 0 <= ly < self.g.H
            pressed = key_down(VK_LBUTTON) or key_down(VK_RBUTTON)
            if not getattr(self, "_click_ready", False):
                # ждём, пока отпустят кнопку после клика по «Отправить»
                self._click_ready = not pressed
                pressed = False
            if pressed and not inside and self.wheel.winfo_viewable():
                self.hide_wheel(close=True)
                return
        except Exception:
            log_error(traceback.format_exc())
        self.root.after(30, self._click_poll)

    # ── выполнение ──
    def run_action(self, action: engine.Action, files: list[Path], exit_after=False):
        import license_ui
        if not license_ui.require_pro(self.root, action, on_change=lambda _l: self.tray_refresh()):
            trace(f"Нужна Pro: {action.id}")
            if exit_after:
                self.root.after(100, self._maybe_exit)
            return
        if action.ui == "organizer":
            self.open_organizer([p for p in files if engine.category(p) in ("pdf", "image")])
            return
        if action.ui and action.ui.startswith("viewer"):
            self.open_viewer(files, tool=action.ui.split(":", 1)[1] if ":" in action.ui else None)
            return
        params = self._ask_params(action, files)
        if params is None:
            trace(f"Отменено: {action.id}")
            if exit_after:
                self.root.after(100, self._maybe_exit)
            return
        name = files[0].name + (f" и ещё {len(files) - 1}" if len(files) > 1 else "")
        verb = f"Конвертирую в {action.label}" if action.id.split("_")[0] in ("img", "aud", "vid", "doc", "pdf") \
            and action.hint.startswith("Конвертировать") else action.hint
        job = {"title": verb, "sub": name, "phase": 0.0, "done": False}
        self._job = job
        self.toast(verb, name, progress="busy", anchor=getattr(self, "card_anchor", None))
        self._animate_job(job)

        def work():
            try:
                res = action.run(files, **params)
                self.events.put(("done", action, res, exit_after, job))
            except engine.EngineError as e:
                self.events.put(("error", action, str(e), exit_after, job))
            except Exception as e:
                log_error(traceback.format_exc())
                self.events.put(("error", action, f"{type(e).__name__}: {e}", exit_after, job))
        threading.Thread(target=work, daemon=True).start()

    def _animate_job(self, job):
        if job["done"] or self._job is not job or self.toast_win is None:
            return
        job["phase"] = (job["phase"] + 0.025) % 1.0
        self._update_card_image(wheel_render.render_card_indeterminate(job["title"], job["sub"], job["phase"], self.k))
        self.root.after(40, lambda: self._animate_job(job))

    def _drain(self):
        try:
            self._drain_events()
        except Exception:
            log_error("drain: " + traceback.format_exc())
        self.root.after(50, self._drain)

    def _drain_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                trace(f"Событие: {kind}")
                if kind == "done":
                    _, action, res, exit_after, job = ev
                    job["done"] = True
                    if res.clipboard is not None and IS_WIN:
                        set_clipboard(res.clipboard)
                    title = f"Готово — {action.label}"
                    if res.outputs:
                        first = res.outputs[0]
                        body = first.name + (f" и ещё {len(res.outputs) - 1}" if len(res.outputs) > 1 else "")
                        if res.message:
                            body += " · " + res.message
                        self.toast(title, body, on_click=lambda p=first: reveal(p), anchor=self._card_pos)
                    else:
                        self.toast(title, res.message or action.hint, anchor=self._card_pos)
                    if exit_after:
                        self.root.after(4500, self._maybe_exit)
                elif kind == "error":
                    _, action, msg, exit_after, job = ev
                    job["done"] = True
                    self.toast("Не получилось", msg, error=True, anchor=self._card_pos)
                    if exit_after:
                        self.root.after(7000, self._maybe_exit)
                elif kind == "tray":
                    self._tray_command(ev[1])
        except queue.Empty:
            pass

    # ── уведомления: светлая карточка, как у Tangerine ──
    def toast(self, title, body, error=False, sticky=False, on_click=None, progress=None, anchor=None):
        from PIL import ImageTk
        tk = self.tk
        if self.toast_win is not None:
            try:
                self.toast_win.destroy()
            except tk.TclError:
                pass
            self.toast_win = None
        link = "Показать в папке" if on_click else ""
        if progress == "busy":
            im = wheel_render.render_card_indeterminate(title, body, 0.0, self.k)
        else:
            im = wheel_render.render_card(title, body, 1.0 if progress is None else progress, self.k,
                                          error=error, link=link)
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=self.key_hex)
        try:
            t.attributes("-transparentcolor", self.key_hex)
        except tk.TclError:
            pass
        c = tk.Canvas(t, width=im.width, height=im.height, bg=self.key_hex, highlightthickness=0, bd=0)
        c.pack()
        photo = ImageTk.PhotoImage(im)
        t._photo = photo
        t._item = c.create_image(0, 0, anchor="nw", image=photo)
        t._canvas = c
        # позиция: рядом с местом, где отпустили файл (как у Tangerine), иначе — в углу экрана
        left, top, right, bottom = work_area() if IS_WIN else (0, 0, t.winfo_screenwidth(), t.winfo_screenheight())
        if anchor:
            ax, ay = anchor
            px = min(max(ax - im.width // 2, left + 8), right - im.width - 8)
            py = ay - im.height - 24
            if py < top + 8:
                py = ay + 24
            py = min(py, bottom - im.height - 8)
        else:
            px, py = right - im.width - 16, bottom - im.height - 16
        self._card_pos = anchor
        t.geometry(f"{im.width}x{im.height}+{int(px)}+{int(py)}")

        def click(e):
            # «×» в левом верхнем углу закрывает, остальное — открывает папку
            if e.x < 40 * self.k and e.y < 40 * self.k:
                t.destroy()
                return
            if on_click:
                on_click()
            t.destroy()
        c.bind("<Button-1>", click)
        self.toast_win = t
        if not sticky and progress != "busy":
            t.after(7000 if error else 4500, lambda: t.winfo_exists() and t.destroy())

    def _update_card_image(self, im):
        from PIL import ImageTk
        t = self.toast_win
        try:
            if t is None or not t.winfo_exists():
                return
            photo = ImageTk.PhotoImage(im)
            t._photo = photo
            t._canvas.itemconfigure(t._item, image=photo)
        except self.tk.TclError:
            pass

    # ── трей ──
    def _start_tray(self):
        try:
            import pystray
        except ImportError:
            return
        from PIL import Image
        image = Image.open(ICON) if ICON.exists() else make_icon_image(64)
        put = lambda cmd: (lambda *_: self.events.put(("tray", cmd)))
        S = self.settings
        menu = pystray.Menu(
            pystray.MenuItem("Открыть Mandarin", put("studio"), default=True),
            pystray.MenuItem("PDF-редактор…", put("viewer")),
            *([] if lic.FREE_BUILD else [pystray.MenuItem(lambda _: lic.status_text(), put("license"))]),
            pystray.MenuItem("Упорядочить PDF…", put("organizer")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Включено", put("toggle_enabled"), checked=lambda _: S["enabled"]),
            pystray.MenuItem("Работать во всех приложениях", put("toggle_everywhere"),
                             checked=lambda _: S["everywhere"]),
            pystray.MenuItem("Инструменты по клавише", pystray.Menu(
                pystray.MenuItem("Ctrl (как ⌘ в Tangerine)", put("tools_ctrl"), radio=True,
                                 checked=lambda _: S.get("tools_key", "ctrl") == "ctrl"),
                pystray.MenuItem("Alt", put("tools_alt"), radio=True,
                                 checked=lambda _: S.get("tools_key", "ctrl") == "alt"))),
            pystray.MenuItem("Запускать вместе с Windows", put("toggle_autostart"),
                             checked=lambda _: IS_WIN and autostart_enabled()),
            pystray.MenuItem("Пункт в меню «Отправить»", put("toggle_sendto"),
                             checked=lambda _: IS_WIN and sendto_link().exists()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Как пользоваться", put("help")),
            pystray.MenuItem("Открыть папку журналов", put("logs")),
            pystray.MenuItem("Выход", put("quit")),
        )
        self.tray = pystray.Icon(APP, image, "Mandarin — конвертер файлов", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def tray_refresh(self):
        try:
            self.tray.update_menu()
        except Exception:
            pass

    def _tray_command(self, cmd):
        S = self.settings
        try:
            if cmd == "studio":
                self.open_studio()
            elif cmd == "organizer":
                self.open_organizer([])
            elif cmd == "license":
                import license_ui
                license_ui.LicenseDialog(self.root, on_change=lambda _l: self.tray_refresh())
            elif cmd == "viewer":
                self.open_viewer([])
            elif cmd == "toggle_enabled":
                S["enabled"] = not S["enabled"]; save_settings(S)
            elif cmd == "toggle_everywhere":
                S["everywhere"] = not S["everywhere"]; save_settings(S)
            elif cmd == "toggle_autostart":
                set_autostart(not autostart_enabled())
            elif cmd == "toggle_sendto":
                set_sendto(not sendto_link().exists())
            elif cmd == "logs":
                subprocess.Popen(["explorer", str(settings_dir())])
            elif cmd in ("tools_ctrl", "tools_alt"):
                S["tools_key"] = cmd.split("_")[1]; save_settings(S)
            elif cmd == "help":
                tools = "Ctrl" if S.get("tools_key", "ctrl") == "ctrl" else "Alt"
                self.toast("Как пользоваться",
                           f"Начните перетаскивать файл и нажмите Shift — откроется колесо форматов, {tools} — "
                           "инструменты. Колесо остаётся открытым, пока вы не отпустите файл. Отпустите на "
                           "нужной плитке — результат появится рядом с оригиналом.", sticky=True)
            elif cmd == "quit":
                try:
                    self.tray.stop()
                except Exception:
                    pass
                self.root.destroy()
                return
            self.tray.update_menu()
        except Exception as e:
            log_error(traceback.format_exc())
            self.toast("Ошибка", str(e), error=True)

    def run(self):
        self.root.mainloop()


def make_icon_image(size: int = 256):
    """Мандарин с листиком — рисуется кодом, без внешних картинок."""
    from PIL import Image, ImageDraw
    s = 4 * size
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = s * 0.08
    d.ellipse((m, m * 2.2, s - m, s - m * 0.6), fill=(255, 138, 31, 255))
    d.ellipse((s * 0.22, s * 0.36, s * 0.42, s * 0.52), fill=(255, 190, 110, 255))   # блик
    d.polygon([(s * 0.50, s * 0.22), (s * 0.80, s * 0.02), (s * 0.92, s * 0.10), (s * 0.62, s * 0.28)],
              fill=(46, 158, 106, 255))                                               # лист
    d.ellipse((s * 0.46, s * 0.17, s * 0.56, s * 0.27), fill=(120, 80, 40, 255))      # черенок
    return im.resize((size, size), Image.LANCZOS)


def no_actions_message(files: list[Path], mode: str) -> tuple[str, str]:
    cat = engine.common_category(files)
    if cat == "doc" and mode == "convert":
        return ("Нужен Office или LibreOffice",
                "Для документов Word/Excel/PowerPoint установите Microsoft Office или бесплатный LibreOffice. "
                "Инструменты (Shift+Alt) и PDF → Word работают без них.")
    if cat == "mixed":
        return "Разные типы файлов", "Выберите файлы одного типа (или только PDF и картинки — их можно объединить)."
    hint = "Попробуйте Shift+Alt — там инструменты." if mode == "convert" else "Попробуйте просто Shift."
    return "Нечего делать", f"Для «{files[0].name}» нет действий в этом режиме. {hint}"


def main():
    if sys.stdout is None:                     # windowed-сборка: у процесса нет консоли
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    args = sys.argv[1:]
    enable_crash_log()
    trace(f"Старт: {' '.join(args) or '(без аргументов)'}")
    set_dpi_aware()
    flags = {a for a in args if a.startswith("--")}
    files = [Path(a) for a in args if not a.startswith("--") and Path(a).exists()]
    if "--menu" in flags:
        if files:
            MandarinApp("menu", files).run()
        return
    if "--organize" in flags:
        MandarinApp("organize", files).run()
        return
    if "--pdf" in flags:
        MandarinApp("pdf", files).run()
        return
    pdfs = [f for f in files if engine.category(f) == "pdf"]
    if not single_instance():
        # Mandarin уже работает в трее: один PDF — редактор, несколько — порядок страниц, иначе — окно
        if len(pdfs) == 1:
            MandarinApp("pdf", pdfs).run()
        elif pdfs:
            MandarinApp("organize", pdfs).run()
        else:
            MandarinApp("studio", files).run()
        return
    first_run()
    MandarinApp("background", files=pdfs[:1] or None, show_studio="--background" not in flags and not pdfs).run()


def first_run() -> None:
    """При первом запуске: автозапуск и пункт «Отправить» (оба выключаются в трее)."""
    s = load_settings()
    if s.get("first_run_done") or not IS_WIN:
        return
    for fn in (lambda: set_autostart(True), lambda: set_sendto(True)):
        try:
            fn()
        except Exception:
            log_error(traceback.format_exc())
    s["first_run_done"] = True
    save_settings(s)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error(traceback.format_exc())
        raise
