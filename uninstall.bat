@echo off
chcp 65001 >nul
rem Полное удаление Mandarin: закрывает программу, убирает автозагрузку,
rem пункт «Отправить», настройки и (после вопроса) папку программы.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" & exit /b
