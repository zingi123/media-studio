; Inno Setup script -> installer\MediaStudio_Setup.exe  (install once per PC; later versions arrive by the in-app update)
[Setup]
AppName=MediaStudio
AppVersion=1.0.0
DefaultDirName={localappdata}\MediaStudio
PrivilegesRequired=lowest
DefaultGroupName=MediaStudio
OutputDir=installer
OutputBaseFilename=MediaStudio_Setup
SetupIconFile=assets\logo.ico
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes

[Files]
Source: "dist\MediaStudio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: "code\*"
; code\ is copied only when missing, so reinstalling never overwrites a newer updated code
Source: "dist\MediaStudio\code\*"; DestDir: "{app}\code"; Flags: recursesubdirs onlyifdoesntexist

[Icons]
Name: "{autoprograms}\MediaStudio"; Filename: "{app}\MediaStudio.exe"
Name: "{autodesktop}\MediaStudio"; Filename: "{app}\MediaStudio.exe"

[Run]
Filename: "{app}\MediaStudio.exe"; Description: "Run MediaStudio"; Flags: nowait postinstall skipifsilent
