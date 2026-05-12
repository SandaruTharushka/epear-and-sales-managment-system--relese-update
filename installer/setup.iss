; setup.iss
; ---------
; Inno Setup 6 script for the Garage / Repair POS installer.
;
; Requirements:
;   - Inno Setup 6.x  (https://jrsoftware.org/isinfo.php)
;   - Run BUILD.bat first to populate dist\GaragePOS\
;
; Build the installer:
;   iscc installer\setup.iss
;
; Output: installer\Output\GaragePOS_Setup_vX.X.X.exe

#define AppName    "Garage POS"
#define AppVersion "1.0.0"
#define AppPublisher "Your Company Name"
#define AppURL     "http://127.0.0.1:5000"
#define ExeName    "GaragePOS.exe"
#define DistDir    "..\dist\GaragePOS"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=GaragePOS_Setup_v{#AppVersion}
SetupIconFile=
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";   Description: "Create a &desktop shortcut";     GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startupbridge"; Description: "Start &Scanner Bridge at login";  GroupDescription: "Scanner Bridge:";   Flags: unchecked

[Files]
; Main application (everything PyInstaller built)
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Ensure config and logs directories exist
Source: "{#DistDir}\config\scanner_devices.json"; DestDir: "{app}\config"; Flags: ignoreversion onlyifdoesntexist

[Dirs]
Name: "{app}\logs";   Permissions: users-modify
Name: "{app}\config"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}";       Filename: "{app}\{#ExeName}"
Name: "{group}\Scanner Bridge";   Filename: "{app}\scanner_bridge.exe"
Name: "{group}\Uninstall";        Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
; Launch the app after install
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; Optionally register scanner bridge autostart
Filename: "reg.exe"; \
  Parameters: "add ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v ScannerBridge /t REG_SZ /d ""{app}\scanner_bridge.exe"" /f"; \
  Flags: runhidden; \
  Tasks: startupbridge

[UninstallRun]
; Remove autostart entry on uninstall
Filename: "reg.exe"; \
  Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v ScannerBridge /f"; \
  Flags: runhidden; \
  RunOnceId: "RemoveScannerBridgeAutostart"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
