"""Подписывает собранный .exe ключом сборки → файл-страж рядом (.sig). Запускается в CI.

    MANDARIN_BUILD_KEY=<base64> python tools/sign_build.py путь/к/Mandarin.exe

Закрытый ключ берётся из переменной окружения MANDARIN_BUILD_KEY (GitHub-секрет). Его открытую
часть программа хранит в license.BUILD_PUBKEY_B64 и при запуске проверяет, что .exe не пропатчен.
"""
import base64
import os
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        sys.exit("Укажите путь к .exe")
    exe = Path(sys.argv[1])
    key = os.environ.get("MANDARIN_BUILD_KEY")
    if not key:
        sys.exit("Нет переменной окружения MANDARIN_BUILD_KEY (GitHub-секрет)")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sig = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key)).sign(exe.read_bytes())
    out = exe.with_name(exe.name + ".sig")
    out.write_bytes(sig)
    print(f"Подписано: {out} ({len(sig)} байт)")


if __name__ == "__main__":
    main()
