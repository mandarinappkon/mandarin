; Mandarin — установщик Windows (Inno Setup 6). Делает обычный Setup.exe:
;   установка без прав администратора, ярлыки, пункт в «Установке и удалении программ».
; Собирается на Windows: iscc installer.iss  (в сборке на GitHub это делается автоматически).
; Перед запуском должна существовать папка dist\Mandarin (её создаёт build.bat или GitHub Actions).

#define AppName "Mandarin"
#define AppVersion "1.0.0"
#define AppExe "Mandarin.exe"

[Setup]
AppId={{469BB6AB-2EE8-42A9-B116-247D57BE2133}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Mandarin
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist-installer
OutputBaseFilename=Mandarin-Setup
SetupIconFile=mandarin.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; всё содержимое собранной папки
Source: "dist\Mandarin\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Удаление {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// При удалении убираем то, что программа создала сама: автозапуск, пункт «Отправить», настройки.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var sendto: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Mandarin');
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Mandarin');
    sendto := ExpandConstant('{userappdata}\Microsoft\Windows\SendTo\Mandarin.lnk');
    if FileExists(sendto) then DeleteFile(sendto);
    DelTree(ExpandConstant('{userappdata}\Mandarin'), True, True, True);
    DelTree(ExpandConstant('{localappdata}\Mandarin'), True, True, True);
  end;
end;
