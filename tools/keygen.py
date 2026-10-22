"""
Генератор ключей Mandarin Pro (только для продавца — НЕ включайте в сборку программы).

    python tools/keygen.py init                 создать пару ключей (один раз!)
    python tools/keygen.py issue почта@пример.ру [--edition 1] [--id номер]
    python tools/keygen.py check КЛЮЧ           проверить ключ

После `init`:
  * seller_private.key — ЗАКРЫТЫЙ ключ. Храните в секрете и сделайте резервную копию: без него
    нельзя выпускать новые ключи, а с ним любой сможет. На сервер продаж он передаётся
    переменной окружения MANDARIN_PRIVATE_KEY.
  * открытый ключ сразу вписывается в license.py (PUBLIC_KEY_B64) — пересоберите программу.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import license as L  # noqa: E402

PRIV = ROOT / "tools" / "seller_private.key"


def cmd_init(args):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if PRIV.exists() and not args.force:
        sys.exit(f"Ключ уже есть: {PRIV}\nПовторное создание сделает все выданные ключи недействительными. "
                 "Если вы уверены: --force")
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    PRIV.write_text(base64.b64encode(priv).decode(), encoding="ascii")
    try:
        os.chmod(PRIV, 0o600)
    except OSError:
        pass
    pub_b64 = base64.b64encode(pub).decode()
    pepper = "mandarin." + secrets.token_hex(12)      # уникальный ключ HMAC для этой сборки
    lic_py = ROOT / "license.py"
    src = lic_py.read_text("utf-8")
    src = re.sub(r'PUBLIC_KEY_B64 = "[^"]*"', f'PUBLIC_KEY_B64 = "{pub_b64}"', src, count=1)
    src = re.sub(r'_PEPPER = b"[^"]*"', f'_PEPPER = b"{pepper}"', src, count=1)
    lic_py.write_text(src, "utf-8")
    print("Готово.")
    print(f"  Закрытый ключ: {PRIV}   ← в секрете, сделайте резервную копию")
    print(f"  Открытый ключ вписан в license.py: {pub_b64}")
    print("  Уникальный ключ защиты пробного периода тоже вписан в license.py.")
    print(f"  Для сервера продаж:  MANDARIN_PRIVATE_KEY={base64.b64encode(priv).decode()}")


def _priv() -> bytes:
    env = os.environ.get("MANDARIN_PRIVATE_KEY")
    if env:
        return base64.b64decode(env)
    if not PRIV.exists():
        sys.exit("Нет закрытого ключа. Сначала: python tools/keygen.py init")
    return base64.b64decode(PRIV.read_text("ascii").strip())


def cmd_issue(args):
    lid = hashlib.sha256(args.id.encode()).digest()[:12] if args.id else secrets.token_bytes(12)
    key = L.make_key(_priv(), lid, args.email, args.edition)
    lic = L.verify_key(key)
    print(key)
    print(f"# {lic.edition_name}, выдан {lic.issued}, № {lic.license_id}, для {args.email}", file=sys.stderr)


def cmd_check(args):
    try:
        lic = L.verify_key(args.key)
    except L.LicenseError as e:
        sys.exit(f"Недействителен: {e}")
    ok = ""
    if args.email:
        ok = " · почта совпадает" if L.email_tag(args.email).hex() == lic.email_tag else " · ПОЧТА НЕ СОВПАДАЕТ"
    print(f"Действителен: {lic.edition_name}, выдан {lic.issued}, № {lic.license_id}{ok}")


def main():
    p = argparse.ArgumentParser(description="Ключи Mandarin Pro")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init")
    a.add_argument("--force", action="store_true")
    a.set_defaults(fn=cmd_init)
    a = sub.add_parser("issue")
    a.add_argument("email")
    a.add_argument("--edition", type=int, default=1, choices=sorted(L.EDITIONS))
    a.add_argument("--id", help="номер заказа (ключ для одного заказа всегда одинаковый)")
    a.set_defaults(fn=cmd_issue)
    a = sub.add_parser("check")
    a.add_argument("key")
    a.add_argument("--email")
    a.set_defaults(fn=cmd_check)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
