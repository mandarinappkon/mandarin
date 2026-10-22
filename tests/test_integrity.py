"""
Файл-страж: проверка подписи .exe. Патч бинарника → Pro по пробному периоду закрыт, но купленный
ключ работает всегда; отсутствие подписи честного пользователя не блокирует. python tests/test_integrity.py
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["APPDATA"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
os.environ["MANDARIN_FREE"] = "0"          # платный режим — защита активна
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import license as L
L._reg_read = lambda: (None, False)
L._reg_write = lambda t: None

# ключ продавца (для купленного ключа)
seller = Ed25519PrivateKey.generate()
SELLER_PRIV = seller.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
L.PUBLIC_KEY_B64 = base64.b64encode(seller.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

# ключ сборки (для подписи .exe)
build = Ed25519PrivateKey.generate()
BUILD_PRIV = build.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
L.BUILD_PUBKEY_B64 = base64.b64encode(build.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

tmp = Path(tempfile.mkdtemp())
exe = tmp / "Mandarin.exe"
exe.write_bytes(b"MZ" + os.urandom(4096))        # как будто собранный бинарник
L._compiled_exe = lambda: exe                     # притворяемся, что запущены из .exe

ok = 0
def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print("  ok ", name)

def reset_trial():
    for p in (L._settings_path(), L._shadow_path()):
        try: p.unlink()
        except OSError: pass

# ── 1. нет файла подписи → не мешаем честному пользователю (fail-open) ──
reset_trial()
check("без .sig страж пропускает (fail-open)", L.integrity_ok() is True)
check("в пробный период Pro открыт", L.pro_enabled() is True)

# ── 2. правильная подпись → всё в порядке ──
sig = exe.with_name(exe.name + ".sig")
sig.write_bytes(build.sign(exe.read_bytes()))
check("корректная подпись проходит", L.integrity_ok() is True)
check("Pro открыт при корректной подписи", L.pro_enabled() is True)

# ── 3. .exe пропатчили (подпись больше не сходится) ──
exe.write_bytes(b"MZ" + os.urandom(4096))         # байты изменились, .sig прежний
check("патч .exe замечен", L.integrity_ok() is False)
check("пропатченный .exe не получает бесплатный Pro", L.pro_enabled() is False)

# ── 4. но купленный ключ работает даже на пропатченном .exe ──
key = L.make_key(SELLER_PRIV, b"order-0001", "buyer@mail.ru")
L.activate(key)
check("купленный ключ действует независимо от стража", L.is_pro() and L.pro_enabled() is True)

# ── 5. присутствует, но не сходится (мусор вместо подписи) → считаем подделкой ──
L.deactivate(); reset_trial()
sig.write_bytes(b"not-a-signature")
check("негодная подпись считается подделкой", L.integrity_ok() is False)

# ── 6. совсем нет файла → fail-open (честный пользователь без .sig не блокируется) ──
sig.unlink()
exe.write_bytes(b"MZ" + os.urandom(4096))
check("без файла подписи — fail-open", L.integrity_ok() is True)

print(f"\nФайл-страж целостности: {ok} ok")
