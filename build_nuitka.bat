@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem ── Усиленная сборка Mandarin через Nuitka: Python компилируется в машинный код ──
rem Нужен Python 3.12. Компилятор C Nuitka при первом запуске предложит скачать сам (MinGW) —
rem согласитесь (ответ Y). Сборка идёт дольше PyInstaller (5–15 минут).

where python >nul 2>nul || (echo Установите Python 3.12 с python.org. & pause & exit /b 1)

python -c "import license,sys; sys.exit(license.PUBLIC_KEY_B64.startswith('REPLACE'))"
if errorlevel 1 (
    echo ВНИМАНИЕ: не настроен ключ продавца — купленные ключи не активируются.
    echo Сначала:  python tools\keygen.py init   ^(см. SALES.md^)
    choice /c YN /m "Всё равно собрать"
    if errorlevel 2 exit /b 1
)

if not exist .venv ( python -m venv .venv || goto :err )
call .venv\Scripts\activate.bat || goto :err
python -m pip install --upgrade pip >nul
pip install -r requirements.txt nuitka || goto :err

python -m nuitka ^
  --standalone ^
  --windows-console-mode=disable ^
  --enable-plugin=tk-inter ^
  --windows-icon-from-ico=mandarin.ico ^
  --include-data-file=mandarin.ico=mandarin.ico ^
  --include-data-dir=tessdata=tessdata ^
  --include-package=tkinterdnd2 ^
  --include-package=pymupdf ^
  --include-package=pdf2docx ^
  --include-package=docx ^
  --include-package=pptx ^
  --include-package=openpyxl ^
  --include-package=pyhanko ^
  --include-package=pyhanko_certvalidator ^
  --include-package=imageio_ffmpeg ^
  --include-package=pillow_heif ^
  --output-filename=Mandarin.exe ^
  --output-dir=dist-nuitka ^
  --assume-yes-for-downloads ^
  app.py || goto :err

copy /y uninstall.bat dist-nuitka\app.dist\ >nul
copy /y uninstall.ps1 dist-nuitka\app.dist\ >nul
echo.
echo Готово: dist-nuitka\app.dist\Mandarin.exe
echo Эта сборка скомпилирована в машинный код — её труднее декомпилировать и пропатчить.
pause
exit /b 0
:err
echo Сборка не удалась — см. сообщения выше.
pause
exit /b 1
