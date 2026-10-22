"""Собирает server/function.zip для загрузки в Yandex Cloud Functions: python tools/pack_function.py"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
dst = ROOT / "server" / "function.zip"
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(ROOT / "server" / "sales.py", "sales.py")
    z.write(ROOT / "license.py", "license.py")
    z.write(ROOT / "server" / "requirements.txt", "requirements.txt")
print(f"Готово: {dst}  (точка входа: sales.handler)")
