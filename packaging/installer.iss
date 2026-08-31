; TradingMaster desktop installer. Bundles the frozen backend exe, the
; launcher, a portable Node runtime, and the Next.js standalone build --
; see packaging/README.md for how those are produced and assembled into
; packaging/dist/ before this script runs.
;
; Build with (from packaging/):
;   "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
;
; Assumes PostgreSQL is already installed and running locally (per the
; scoping decision in this project) -- the app falls back to a local
; SQLite file if no DATABASE_URL is configured via a .env next to the exe.

#define MyAppName "TradingMaster"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "TradingMaster"
#define MyAppExeName "tradingmaster-launcher.exe"

[Setup]
AppId={{B6A6F1B0-6C3E-4E9C-9C7B-2B6E6C3E9F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=TradingMasterSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\tradingmaster-backend.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\tradingmaster-launcher.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\node\*"; DestDir: "{app}\node"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\frontend\*"; DestDir: "{app}\frontend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
