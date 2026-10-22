"""Проверка ключей и пробного периода: python tests/test_license.py"""
import base64, datetime as dt, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["APPDATA"] = tempfile.mkdtemp()
os.environ["MANDARIN_FREE"] = "0"   # проверяем платный режим с блокировками
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L

k = Ed25519PrivateKey.generate()
priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
L.PUBLIC_KEY_B64 = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
ok = 0
def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print("  ok ", name)

key = L.make_key(priv, b"order-123456", "Ivan@Mail.ru")
lic = L.verify_key(key)
check(f"ключ выпускается и проверяется ({len(key)} симв.)", lic.edition == 1 and lic.issued == dt.date.today())
check("отпечаток почты без учёта регистра", lic.email_tag == L.email_tag("ivan@mail.ru").hex())
check("ключ детерминирован для заказа", key == L.make_key(priv, b"order-123456", "ivan@mail.ru"))
messy = "  " + key.lower().replace("-", " ").replace("o", "0", 1) + "\n"
check("ключ из письма с пробелами/переносами/опечаткой 0→O", L.verify_key(messy).license_id == lic.license_id)
def bad(s):
    try: L.verify_key(s); return False
    except L.LicenseError: return True
check("подделка отклоняется", bad(key[:-3] + ("AAA" if key[-3:] != "AAA" else "BBB")))
other = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
check("ключ чужого продавца отклоняется", bad(L.make_key(other, b"x" * 12, "a@b.ru")))
check("обрывок ключа отклоняется", bad(key[:40]))

class A:  # действие
    def __init__(s, id, ui=None): s.id, s.ui = id, ui
check("пробный период: всё открыто", L.trial_days_left() == 14 and not L.needs_pro(A("pdf_ocr")))

def set_state(install_days_ago, seen_days_ago=0, tamper=False):
    t = dt.date.today()
    L._persist({"install": (t - dt.timedelta(days=install_days_ago)).isoformat(),
                "seen": (t - dt.timedelta(days=seen_days_ago)).isoformat(), "key": None, "tamper": tamper})

set_state(20)
check("после пробного периода Pro закрыта", L.trial_days_left() == 0 and L.needs_pro(A("pdf_ocr")))
check("…а бесплатное открыто", not L.needs_pro(A("img_png")) and not L.needs_pro(A("organize", "organizer")))
check("PDF-редактор — Pro", L.needs_pro(A("pdf_open", "viewer")) and L.needs_pro(A("x", "viewer:sign")))
set_state(0)
L.activate(messy)
check("активация открывает Pro", L.is_pro() and not L.needs_pro(A("pdf_ocr")))
check("статус", "Pro" in L.status_text())
L.deactivate()
check("отвязка", not L.is_pro())
print(f"\nЛицензии: {ok} ok")
