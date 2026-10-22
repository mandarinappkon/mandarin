# Mandarin — полное удаление с компьютера.
# Запускается через uninstall.bat. Работает и из папки исходников, и из собранной папки dist\Mandarin.
$ErrorActionPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = 'Удаление Mandarin'
$here = $PSScriptRoot

Write-Host ''
Write-Host '=== Удаление Mandarin ===' -ForegroundColor DarkYellow
Write-Host ''

# ── 1. Где установлена программа: по автозагрузке и по ярлыку «Отправить» ──
$runKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$sendTo  = Join-Path $env:APPDATA 'Microsoft\Windows\SendTo\Mandarin.lnk'
$found   = @()
$runCmd  = (Get-ItemProperty -Path $runKey -Name Mandarin).Mandarin
if ($runCmd) {
    if ($runCmd -match '^"([^"]+)"') { $found += $Matches[1] } else { $found += ($runCmd -split ' ')[0] }
}
if (Test-Path $sendTo) {
    $found += (New-Object -ComObject WScript.Shell).CreateShortcut($sendTo).TargetPath
}

# ── 2. Закрываем программу ──
$stopped = 0
Get-Process -Name Mandarin | ForEach-Object { Stop-Process -Id $_.Id -Force; $stopped++ }
# запуск из исходников (run-dev.bat): pythonw/python с app.py из папки mandarin
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'mandarin' -and $_.CommandLine -match '(app|organizer|studio)\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $stopped++ }
if ($stopped) { Start-Sleep -Milliseconds 800; Write-Host "[ok] Программа закрыта ($stopped проц.)" -ForegroundColor Green }
else          { Write-Host '[--] Программа не была запущена' -ForegroundColor DarkGray }

# ── 3. Автозагрузка, «Отправить», настройки ──
if ($runCmd) { Remove-ItemProperty -Path $runKey -Name Mandarin; Write-Host '[ok] Убрана из автозагрузки' -ForegroundColor Green }
else         { Write-Host '[--] В автозагрузке не было' -ForegroundColor DarkGray }

if (Test-Path $sendTo) { Remove-Item $sendTo -Force; Write-Host '[ok] Убран пункт «Отправить → Mandarin»' -ForegroundColor Green }
else                   { Write-Host '[--] Пункта «Отправить» не было' -ForegroundColor DarkGray }

$settings = Join-Path $env:APPDATA 'Mandarin'
if (Test-Path $settings) { Remove-Item $settings -Recurse -Force; Write-Host '[ok] Удалены настройки и журнал (%APPDATA%\Mandarin)' -ForegroundColor Green }
else                     { Write-Host '[--] Папки настроек не было' -ForegroundColor DarkGray }

# ── 4. Папки программы (только с подтверждением) ──
$candidates = @()
foreach ($p in $found) {
    if ($p -and ($p -like '*\Mandarin.exe') -and (Test-Path $p)) { $candidates += (Split-Path $p -Parent) }
}
if (Test-Path (Join-Path $here 'Mandarin.exe')) { $candidates += $here }                      # запущено из собранной папки
foreach ($sub in 'dist\Mandarin') {
    $d = Join-Path $here $sub
    if (Test-Path (Join-Path $d 'Mandarin.exe')) { $candidates += $d }
}
$candidates = $candidates | ForEach-Object { (Resolve-Path $_).Path.TrimEnd('\') } | Sort-Object -Unique

$selfDelete = $null
Set-Location $env:TEMP
foreach ($dir in $candidates) {
    # защита: удаляем только папку, где действительно лежит Mandarin.exe, и не корень диска
    if (-not (Test-Path (Join-Path $dir 'Mandarin.exe')) -or $dir.Length -le 3) { continue }
    Write-Host ''
    $ans = Read-Host "Удалить папку программы `"$dir`"? (Y/N)"
    if ($ans -notmatch '^[YyДд]') { Write-Host '[--] Оставлена' -ForegroundColor DarkGray; continue }
    if ($dir -eq $here.TrimEnd('\')) { $selfDelete = $dir; continue }   # удалим после выхода
    Remove-Item $dir -Recurse -Force
    if (Test-Path $dir) { Write-Host "[!!] Не удалось удалить полностью: $dir" -ForegroundColor Red }
    else                { Write-Host "[ok] Удалена: $dir" -ForegroundColor Green }
}

# служебные папки сборки, если запускали из исходников
if (Test-Path (Join-Path $here 'app.py')) {
    $junk = 'dist', 'build', '.venv', '__pycache__' | ForEach-Object { Join-Path $here $_ } | Where-Object { Test-Path $_ }
    $spec = Join-Path $here 'Mandarin.spec'
    if ($junk -or (Test-Path $spec)) {
        Write-Host ''
        $ans = Read-Host 'Удалить файлы сборки (dist, build, .venv) в папке исходников? Сами исходники останутся. (Y/N)'
        if ($ans -match '^[YyДд]') {
            $junk | ForEach-Object { Remove-Item $_ -Recurse -Force }
            Remove-Item $spec -Force
            Write-Host '[ok] Файлы сборки удалены' -ForegroundColor Green
        }
    }
}

Write-Host ''
Write-Host 'Готово. Mandarin удалён.' -ForegroundColor DarkYellow
if ($selfDelete) {
    Write-Host "Папка `"$selfDelete`" удалится сразу после закрытия этого окна." -ForegroundColor DarkGray
}
Read-Host 'Нажмите Enter, чтобы закрыть'

if ($selfDelete) {
    # папку, из которой запущен скрипт, удаляем отдельным процессом после выхода
    Start-Process cmd.exe -ArgumentList "/c timeout /t 2 /nobreak >nul & rd /s /q `"$selfDelete`"" -WindowStyle Hidden
}
