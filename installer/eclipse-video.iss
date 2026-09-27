#ifndef AppVersion
#define AppVersion "0.0.0"
#endif
#define AppName "Eclipse Video"
#define AppExe "Eclipse Video.exe"

[Setup]
AppId={{4BAE8B10-F4E4-45AD-9804-591E77EF6959}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=riftkuro
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=Eclipse Video Setup {#AppVersion}
SetupIconFile=..\assets\eclipse-app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Eclipse Video\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; earlier builds shipped readable source next to a bundled python runtime
[InstallDelete]
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\*.py"
Type: files; Name: "{app}\Eclipse Video.pyw"
Type: files; Name: "{app}\requirements.txt"
Type: files; Name: "{autodesktop}\{#AppName}.lnk"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
