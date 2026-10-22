@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === Mandarin: сборка Windows-приложения ===
where python >nul 2>nul || (echo Не найден Python. Установите Python 3.11-3.13 с python.org ^(галочка "Add to PATH"^). & pause & exit /b 1)

rem tkdnd (перетаскивание) надёжно работает с Tk 8.6 — это Python 3.10–3.13. В 3.14 уже Tk 9.
for /f %%v in ('python -c "import tkinter;print(int(tkinter.TkVersion))"') do set TKV=%%v
if "%TKV%"=="9" (
    echo.
    echo ВНИМАНИЕ: у вас Python с Tk 9 ^(Python 3.14+^). Перетаскивание может падать.
    echo Установите Python 3.12 с python.org и запустите build.bat снова.
    echo.
    pause
    exit /b 1
)

if not exist .venv (
    python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :err

rem По умолчанию собирается БЕСПЛАТНАЯ полная версия (все функции открыты, без пробного периода).
rem Для платной версии с ключами: заранее выполните  set MANDARIN_FREE=0  (см. SALES.md).
if not defined MANDARIN_FREE set MANDARIN_FREE=1
if "%MANDARIN_FREE%"=="0" (
    python -c "import license,sys; sys.exit(license.PUBLIC_KEY_B64.startswith('REPLACE'))"
    if errorlevel 1 (
        echo.
        echo ВНИМАНИЕ: платная сборка, но не настроен ключ продавца — купленные ключи не активируются.
        echo Один раз выполните:  python tools\keygen.py init   ^(см. SALES.md^)
        echo.
        choice /c YN /m "Всё равно собрать"
        if errorlevel 2 exit /b 1
    )
) else (
    echo Режим: бесплатная полная версия ^(все функции открыты^).
)

pyinstaller --noconfirm --clean --windowed --name Mandarin --icon mandarin.ico ^
  --add-data "mandarin.ico;." ^
  --add-data "tessdata;tessdata" ^
  --collect-all tkinterdnd2 ^
  --collect-all imageio_ffmpeg ^
  --collect-all pillow_heif ^
  --collect-all pymupdf ^
  --collect-all pdf2docx ^
  --collect-all docx ^
  --collect-all pptx ^
  --collect-all openpyxl ^
  --collect-all pyhanko ^
  --collect-all pyhanko_certvalidator ^
  --hidden-import pystray._win32 ^
  app.py || goto :err

copy /y uninstall.bat dist\Mandarin\ >nul
copy /y uninstall.ps1 dist\Mandarin\ >nul

echo.
echo Готово: dist\Mandarin\Mandarin.exe
echo Папку dist\Mandarin можно перенести куда угодно и запускать Mandarin.exe без установки.
echo Удалить программу полностью: uninstall.bat в той же папке.

rem ── установщик Setup.exe (если установлен Inno Setup) ──
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\iscc.exe"
if exist "%ISCC%" (
    "%ISCC%" installer.iss && echo Установщик: dist-installer\Mandarin-Setup.exe
) else (
    echo.
    echo Чтобы собрать установщик Setup.exe, поставьте бесплатный Inno Setup:
    echo   winget install JRSoftware.InnoSetup
    echo и запустите build.bat снова. Без него работает портативная папка dist\Mandarin.
)

rem ── необязательно: Ghostscript для «Сохранить как PDF/A» ──
where gswin64c >nul 2>nul && goto :done
if exist "%ProgramFiles%\gs" goto :done
echo.
echo Для функции PDF/A (архивный формат) нужен бесплатный Ghostscript. Остальное работает без него.
where winget >nul 2>nul || goto :nowinget
choice /c YN /m "Установить Ghostscript сейчас через winget"
if errorlevel 2 goto :done
winget install --id ArtifexSoftware.GhostScript -e --accept-source-agreements --accept-package-agreements
goto :done
:nowinget
echo Скачать можно с https://ghostscript.com/releases/gsdnld.html
:done
pause
exit /b 0

:err
echo.
echo Сборка не удалась — см. сообщения выше.
pause
exit /b 1
