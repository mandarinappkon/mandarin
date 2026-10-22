"""
Mandarin — лицензии: ключи Pro, пробный период, проверка без интернета.

Ключ — это подписанная запись (Ed25519): номер лицензии, редакция, дата выдачи и отпечаток
почты покупателя. В программе хранится только ОТКРЫТЫЙ ключ продавца, поэтому подделать
лицензию, разобрав программу, нельзя; закрытый ключ есть только у продавца (сервер продаж
и tools/keygen.py).

Что бесплатно и что Pro — см. PRO_ACTIONS. В пробный период (TRIAL_DAYS) открыто всё.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

# ── секреты сборки ──────────────────────────────────────────────────────────────────
# Режим сборки и «перец» HMAC берутся из _secret.py, который создаётся ПРИ СБОРКЕ из GitHub-
# секретов и НЕ хранится в репозитории (см. .gitignore). В разработке и тестах — из переменных
# окружения или безопасных значений по умолчанию. Так настоящий перец не виден в исходном коде,
# и прочитать его «через GPT», открыв репозиторий, нельзя.
try:
    import _secret as _S            # создаётся CI: FREE="0", PEPPER="…"
except Exception:
    _S = None


def _cfg(env_name: str, secret_name: str, default: str) -> str:
    v = os.environ.get(env_name)
    if v is not None:
        return v
    if _S is not None and hasattr(_S, secret_name):
        return str(getattr(_S, secret_name))
    return default


# ── режим сборки ────────────────────────────────────────────────────────────────────
# True  — бесплатная полная версия: ВСЕ функции открыты, нет пробного периода, ключей и блокировок.
# False — платная версия: после пробного периода PDF-функции требуют ключ (см. SALES.md).
FREE_BUILD = _cfg("MANDARIN_FREE", "FREE", "1") != "0"

# ── настройки продавца (нужны только для платной версии) ─────────────────────────────
# Открытый ключ продавца (base64, 32 байта). Замените своим: python tools/keygen.py init
PUBLIC_KEY_B64 = "FXHBQH0glw9tzfQjxHus7N0LVibzpP/uWb436+UFYVA="
# Открытый ключ проверки целостности сборки (файл-страж Mandarin.exe.sig). Закрытая часть —
# только в GitHub-секрете MANDARIN_BUILD_KEY, которым CI подписывает .exe при сборке.
BUILD_PUBKEY_B64 = "ZiLOKlS42QPoz+A9aMM0d2KFjVJFCSG3iTFQBXbUF7A="
BUY_URL = "https://mandarin-app.ru/#buy"         # страница покупки
SUPPORT_EMAIL = "support@mandarin-app.ru"
TRIAL_DAYS = 14

PREFIX = "MNDR"
EDITIONS = {1: "Pro", 2: "Pro для бизнеса"}
EPOCH = _dt.date(2024, 1, 1)

# Действия, которые после пробного периода требуют Pro (всё из «PDF Pro»).
# Конвертация картинок, аудио, видео, документов, PDF → Word/PNG/JPG/TXT,
# сжатие/разбиение/поворот PDF и «Упорядочить» остаются бесплатными.
PRO_ACTIONS = {
    "pdf_open", "pdf_sign", "pdf_protect", "pdf_unlock", "pdf_ocr", "pdf_redact", "pdf_watermark",
    "pdf_numbers", "pdf_headers", "pdf_bates", "pdf_crop", "pdf_compare", "pdf_cert", "pdf_verify",
    "pdf_flatten", "pdf_xlsx", "pdf_pptx", "pdf_pdfa",
}
PRO_UI = {"viewer"}          # окна, которые требуют Pro (PDF-редактор)


# ── формат ключа ────────────────────────────────────────────────────────────────────
# payload (20 байт): версия(1) редакция(1) день выдачи от EPOCH(2) номер лицензии(12) отпечаток почты(4)
# ключ = PREFIX + base32(payload + подпись 64 байта), разбитый на группы по 6 символов

_PAYLOAD = struct.Struct(">BBH12s4s")


class LicenseError(Exception):
    pass


@dataclass
class License:
    edition: int
    issued: _dt.date
    license_id: str
    email_tag: str

    @property
    def edition_name(self) -> str:
        return EDITIONS.get(self.edition, "Pro")


def email_tag(email: str) -> bytes:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).digest()[:4]


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=")


def _unb32(s: str) -> bytes:
    s = s.upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def format_key(raw: bytes) -> str:
    body = _b32(raw)
    return PREFIX + "-" + "-".join(body[i:i + 6] for i in range(0, len(body), 6))


def normalize_key(text: str) -> str:
    """Чистит ключ после копирования из письма: пробелы, переносы, лишние символы, похожие буквы."""
    s = "".join(ch for ch in text.upper() if ch.isalnum())
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]
    # в base32 нет 0/1/8 — частые опечатки при ручном вводе
    return s.translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))


def make_key(private_key_bytes: bytes, license_id: bytes, email: str, edition: int = 1,
             issued: _dt.date | None = None) -> str:
    """Выпуск ключа (только у продавца). license_id — 12 байт, например из номера платежа."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    issued = issued or _dt.date.today()
    payload = _PAYLOAD.pack(1, edition, (issued - EPOCH).days, license_id[:12].ljust(12, b"\0"), email_tag(email))
    sig = Ed25519PrivateKey.from_private_bytes(private_key_bytes).sign(payload)
    return format_key(payload + sig)


def verify_key(text: str, public_key_b64: str | None = None) -> License:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = public_key_b64 or PUBLIC_KEY_B64
    if pub.startswith("REPLACE"):
        raise LicenseError("Ключ продавца не настроен в этой сборке")
    try:
        raw = _unb32(normalize_key(text))
    except Exception:
        raise LicenseError("Ключ введён с ошибкой — скопируйте его из письма целиком")
    if len(raw) != _PAYLOAD.size + 64:
        raise LicenseError("Ключ введён не полностью — скопируйте его из письма целиком")
    payload, sig = raw[:_PAYLOAD.size], raw[_PAYLOAD.size:]
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(sig, payload)
    except InvalidSignature:
        raise LicenseError("Ключ недействителен")
    ver, edition, days, lid, tag = _PAYLOAD.unpack(payload)
    if ver != 1:
        raise LicenseError("Ключ для другой версии программы")
    return License(edition, EPOCH + _dt.timedelta(days=days), lid.hex(), tag.hex())


# ── защищённое хранение состояния пробного периода ──────────────────────────────────
# Состояние пробного периода хранится в НЕСКОЛЬКИХ местах (файл настроек, реестр, скрытый
# файл) и подписано HMAC. Это защищает от трёх самых частых способов обойти пробный период:
#   1. Отредактировать дату в license.json       → подпись перестаёт сходиться, запись игнорируется.
#   2. Удалить настройки и начать заново           → копия в реестре и втором файле сохраняется.
#   3. Перевести системные часы назад              → храним «самую позднюю виденную дату»; пробный
#                                                     период считается от неё, а не от текущей даты.
# Любая из этих попыток выставляет «липкий» флаг tamper: пробный период замораживается навсегда,
# снять его можно только покупкой ключа. Честному пользователю со сбитыми часами это не грозит —
# у «виденной даты» есть допуск в 2 дня, а купленный ключ (Ed25519) работает НЕЗАВИСИМО от всего
# этого и под защиту не попадает.
#
# Потолок защиты честный: HMAC-перец лежит внутри программы, и подготовленный злоумышленник,
# разобрав её, сможет подделать запись. Задача — не «сделать невзламываемо» (так не бывает), а
# закрыть простые способы, которыми пробный период продлевают обычные пользователи.

_PEPPER = _cfg("MANDARIN_PEPPER", "PEPPER", "mandarin.dev.unsigned").encode()   # настоящий перец вшивается при сборке из секрета
_TOLERANCE = 2                               # дней: запас на часовые пояса и сбитые часы


def _settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "Mandarin"
    d.mkdir(parents=True, exist_ok=True)
    return d / "license.json"


def _shadow_path() -> Path:
    """Второй файл, подальше от настроек: удаление настроек его не трогает."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "Mandarin" / "cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d / ".mnd"


def machine_id() -> str:
    """Отпечаток компьютера: привязывает подпись к этой машине, чтобы подписанную запись нельзя
    было скопировать на другой компьютер и так перенести «свежий» пробный период."""
    parts = [os.environ.get("USERNAME") or os.environ.get("USER") or "?"]
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                parts.append(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            parts.append(os.environ.get("COMPUTERNAME", ""))
    else:
        try:
            parts.append(Path("/etc/machine-id").read_text().strip())
        except OSError:
            parts.append(os.uname().nodename)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _sign(rec: dict) -> str:
    msg = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_PEPPER + machine_id().encode(), msg, hashlib.sha256).hexdigest()


def _wrap(rec: dict) -> str:
    return json.dumps({"d": rec, "s": _sign(rec)}, ensure_ascii=False)


def _unwrap(text: str) -> tuple[dict | None, bool]:
    """(запись, подпись_верна). Если текст есть, но подпись не сходится → (None, True): замечена порча."""
    try:
        obj = json.loads(text)
        rec, sig = obj["d"], obj["s"]
    except Exception:
        return None, False
    if not hmac.compare_digest(sig, _sign(rec)):
        return None, True            # запись подделана или перенесена с другого компьютера
    return rec, False


def _read_location(path: Path) -> tuple[dict | None, bool]:
    try:
        return _unwrap(path.read_text("utf-8"))
    except OSError:
        return None, False


def _reg_read() -> tuple[dict | None, bool]:
    if os.name != "nt":
        return None, False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Mandarin") as k:
            return _unwrap(winreg.QueryValueEx(k, "State")[0])
    except OSError:
        return None, False


def _reg_write(text: str) -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Mandarin") as k:
            winreg.SetValueEx(k, "State", 0, winreg.REG_SZ, text)
    except OSError:
        pass


def _blank() -> dict:
    today = _dt.date.today().isoformat()
    return {"install": today, "seen": today, "key": None, "tamper": False}


def _collect() -> tuple[dict, bool]:
    """Собирает состояние из всех мест. Возвращает (запись, замечена_ли_порча)."""
    recs, tampered = [], False
    for rec, bad in (_read_location(_settings_path()), _reg_read(), _read_location(_shadow_path())):
        tampered = tampered or bad
        if rec:
            recs.append(rec)
    if not recs:
        # Ни одной читаемой записи. Если файлы просто отсутствовали (tampered=False) — это честный
        # первый запуск, начинаем пробный период. Если они БЫЛИ, но с битой подписью (tampered=True) —
        # кто-то их переписал: не выдаём свежий пробный период, а замораживаем.
        return {**_blank(), "tamper": tampered}, tampered
    today = _dt.date.today()

    def d(s, default):
        try:
            return _dt.date.fromisoformat(s)
        except (ValueError, TypeError):
            return default
    # install — самый ранний (нельзя притвориться, что «только что поставил»)
    install = min(d(r.get("install"), today) for r in recs)
    # seen — самая поздняя виденная дата (защита от перевода часов назад)
    seen = max(d(r.get("seen"), install) for r in recs)
    # перевод часов назад больше допуска — считаем попыткой обхода
    if today < seen - _dt.timedelta(days=_TOLERANCE):
        tampered = True
    key = next((r.get("key") for r in recs if r.get("key")), None)
    tamper = tampered or any(r.get("tamper") for r in recs)
    seen = max(seen, today)          # двигаем «виденную дату» вперёд, назад она не ходит
    return {"install": install.isoformat(), "seen": seen.isoformat(), "key": key, "tamper": tamper}, tampered


def _persist(rec: dict) -> None:
    text = _wrap(rec)
    try:
        _settings_path().write_text(text, "utf-8")
    except OSError:
        pass
    _reg_write(text)
    # теневой файл помечается «скрытым»; перезаписать скрытый файл обычным open('w') Windows не даёт,
    # поэтому пишем во временный и подменяем через os.replace (он работает и со скрытым файлом).
    try:
        p = _shadow_path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, "utf-8")
        os.replace(tmp, p)
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x2)   # FILE_ATTRIBUTE_HIDDEN
    except OSError:
        try:
            tmp.unlink()
        except Exception:
            pass


def _state() -> dict:
    """Текущее состояние, синхронизированное во все хранилища (вызывается при каждом обращении)."""
    rec, _ = _collect()
    _persist(rec)
    return rec


def current() -> License | None:
    # Ключ проверяется ТОЛЬКО по подписи Ed25519 и не зависит от защиты пробного периода:
    # честный покупатель не может быть заблокирован, даже если состояние пробного периода испорчено.
    key = _state().get("key")
    if not key:
        return None
    try:
        return verify_key(key)
    except LicenseError:
        return None


def activate(text: str) -> License:
    lic = verify_key(text)                        # бросит LicenseError, если ключ недействителен
    rec = _state()
    rec["key"] = format_key(_unb32(normalize_key(text)))
    _persist(rec)
    return lic


def deactivate() -> None:
    rec = _state()
    rec["key"] = None
    _persist(rec)


def trial_start() -> _dt.date:
    return _dt.date.fromisoformat(_state()["install"])


def tampered() -> bool:
    return _state().get("tamper", False)


def trial_days_left() -> int:
    rec = _state()
    if rec.get("tamper"):
        return 0                                  # замечена подделка — пробный период заморожен
    install = _dt.date.fromisoformat(rec["install"])
    seen = _dt.date.fromisoformat(rec["seen"])    # не раньше сегодняшней: часы назад не помогают
    return max(0, TRIAL_DAYS - (seen - install).days)


def is_pro() -> bool:
    return current() is not None


def _compiled_exe() -> Path | None:
    """Путь к запущенному скомпилированному .exe (PyInstaller/Nuitka), либо None в режиме разработки."""
    import sys
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable)
    return None


def integrity_ok() -> bool:
    """Файл-страж: рядом с .exe лежит подпись Mandarin.exe.sig, сделанная ключом сборки. Если .exe
    пропатчили — подпись не сойдётся. Политика безопасная для честных пользователей: при отсутствии
    файла, ошибке чтения или в режиме разработки возвращаем True (не мешаем); блокируем ТОЛЬКО при
    явном несовпадении подписи. Купленный ключ при этом всё равно работает (см. pro_enabled)."""
    exe = _compiled_exe()
    if exe is None or BUILD_PUBKEY_B64.startswith("REPLACE"):
        return True
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        sig = exe.with_name(exe.name + ".sig").read_bytes()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(BUILD_PUBKEY_B64)).verify(sig, exe.read_bytes())
        return True
    except InvalidSignature:
        return False
    except Exception:
        return True


def pro_enabled() -> bool:
    """Pro-функции доступны: бесплатная сборка, действующий ключ или честный пробный период."""
    if FREE_BUILD:
        return True
    if is_pro():                 # купленный ключ (Ed25519) работает всегда, независимо от защиты
        return True
    if not integrity_ok():       # .exe пропатчен → бесплатный Pro по пробному периоду не выдаём
        return False
    return trial_days_left() > 0


def needs_pro(action) -> bool:
    """True, если действие заблокировано (Pro, а лицензии и пробного периода нет)."""
    if FREE_BUILD:
        return False
    aid = getattr(action, "id", action)
    ui = (getattr(action, "ui", None) or "").split(":")[0]
    is_pro_action = aid in PRO_ACTIONS or ui in PRO_UI
    return is_pro_action and not pro_enabled()


def status_text() -> str:
    if FREE_BUILD:
        return "Mandarin — бесплатная версия, все функции открыты"
    lic = current()
    if lic:
        return f"Mandarin {lic.edition_name} · лицензия от {lic.issued.strftime('%d.%m.%Y')}"
    if tampered():
        return "Пробный период завершён · для Pro-функций нужен ключ"
    left = trial_days_left()
    if left > 0:
        return f"Пробный период Pro: осталось {left} дн."
    return "Бесплатная версия · Pro-функции PDF заблокированы"
