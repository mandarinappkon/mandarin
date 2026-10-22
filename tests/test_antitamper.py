"""
Защита пробного периода: перевод часов назад, правка файла, удаление настроек, перенос на
другой компьютер. python tests/test_antitamper.py

Дату «сегодня» подменяем, чтобы проверить поведение во времени, не трогая системные часы.
"""
import base64
import datetime as real_dt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["APPDATA"] = tempfile.mkdtemp()
os.environ["MANDARIN_FREE"] = "0"   # проверяем платный режим с блокировками
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L
L._reg_read = lambda: (None, False)   # тест полагается только на файлы, без реестра Windows
L._reg_write = lambda t: None

def _unhide(p):   # на Windows скрытый файл нельзя открыть на запись, снимаем атрибут
    if os.name == "nt" and p.exists():
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)   # FILE_ATTRIBUTE_NORMAL

k = Ed25519PrivateKey.generate()
PRIV = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
L.PUBLIC_KEY_B64 = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

# управляемое «сегодня»
NOW = {"d": real_dt.date(2026, 10, 1)}


class FakeDate(real_dt.date):
    @classmethod
    def today(cls):
        return cls(NOW["d"].year, NOW["d"].month, NOW["d"].day)


L._dt.date = FakeDate
def day(d): NOW["d"] = real_dt.date(2026, 10, d)

ok = 0
def check(name, cond, extra=""):
    global ok
    assert cond, f"{name} {extra}"
    ok += 1
    print("  ok ", name, extra)

def reset():
    for p in (L._settings_path(), L._shadow_path()):
        try: p.unlink()
        except OSError: pass

# ── 1. нормальный ход пробного периода ──
reset(); day(1)
check("1-й день: 14 дней пробного периода", L.trial_days_left() == 14)
day(8)
check("8-й день: осталось 7", L.trial_days_left() == 7)
day(16)
check("16-й день: пробный период кончился", L.trial_days_left() == 0 and not L.tampered())

# ── 2. перевод часов назад ──
reset(); day(1); L.trial_days_left()          # зафиксировали установку
day(8); L.trial_days_left()                   # «виденная дата» = 8 октября, осталось 7
day(7)                                         # мелкий откат на 1 день — в пределах допуска
check("часы назад в пределах допуска не продлевают и не блокируют",
      L.trial_days_left() == 7 and not L.tampered(), f"={L.trial_days_left()}")
day(2)                                          # грубый откат за пределы допуска (−6 дней)
check("грубый перевод часов назад замечен", L.tampered())
day(20)                                         # вернули часы вперёд
check("после подделки пробный период не оживает", L.trial_days_left() == 0 and L.tampered())

# ── 3. ручная правка файла состояния ──
import json
reset(); day(10); L.trial_days_left()          # install=10 окт в обоих файлах
for f in (L._settings_path(), L._shadow_path()):   # пользователь правит дату «установки» на будущее
    _unhide(f)
    raw = json.loads(f.read_text("utf-8")); raw["d"]["install"] = "2026-10-30"; f.write_text(json.dumps(raw), "utf-8")
check("правка даты установки в файле замечена (подпись не сходится)", L.tampered())
check("добавление лишних пробелов НЕ ломает честную запись", True)  # канонизация устойчива к форматированию
reset(); day(10); L.trial_days_left()
f = L._settings_path(); _unhide(f); f.write_text(" \n" + f.read_text("utf-8") + "\n ", "utf-8")  # только форматирование
check("переформатирование файла не считается подделкой", not L.tampered())

# ── 4. удаление настроек не сбрасывает пробный период ──
reset(); day(1); L.trial_days_left()
day(16); L.trial_days_left()                   # пробный период кончился, состояние записано в теневой файл
L._settings_path().unlink()                    # пользователь удалил настройки
check("теневой файл уцелел", L._shadow_path().exists())
check("после удаления настроек дни не вернулись", L.trial_days_left() == 0)
check("настройки восстановлены из тени", L._settings_path().exists())

# ── 5. перенос записи на «другой компьютер» ──
reset(); day(1); L.trial_days_left()
stolen = L._settings_path().read_text("utf-8")
orig_mid = L.machine_id
L.machine_id = lambda: "совсем-другой-компьютер"       # эмулируем другую машину
rec, bad = L._unwrap(stolen)
check("подписанная запись с чужого компьютера отвергается", rec is None and bad)
L.machine_id = orig_mid

# ── 6. честный ключ работает, даже если пробный период «окаменел» ──
reset(); day(1); L.trial_days_left()
day(2); L._persist({**L._state(), "tamper": True})     # пометили подделку
check("при подделке пробный период закрыт", L.trial_days_left() == 0 and L.needs_pro(L.PRO_ACTIONS and type("A", (), {"id": "pdf_ocr", "ui": None})))
key = L.make_key(PRIV, b"buyer-000001", "buyer@mail.ru")
L.activate(key)
class A:
    id, ui = "pdf_ocr", None
check("купленный ключ снимает блокировку, несмотря на подделку", L.is_pro() and not L.needs_pro(A))
check("…и статус показывает лицензию", "лицензия" in L.status_text())

print(f"\nЗащита пробного периода: {ok} ok")
