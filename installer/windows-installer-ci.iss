; Unsigned Inno Setup script for CI-built OCCLUDE-edition Windows packages.
;
; The official windows-installer.iss needs OpenShot Studios' code-signing key
; (SignedUninstaller) and the build server's directory layout, so CI uses this
; simplified script instead: it packages an already-assembled application
; directory (the official OpenShot build overlaid with this fork's sources —
; see .github/workflows/windows-package.yml).
;
; Compile with:
;   ISCC.exe /DVERSION=2.6.1 /DSOURCE_DIR=..\base\app windows-installer-ci.iss

#ifndef VERSION
  #define VERSION "0.0.0"
#endif
#ifndef SOURCE_DIR
  #define SOURCE_DIR "..\base\app"
#endif

#define MyAppName "OpenShot Video Editor (OCCLUDE)"
#define MyAppExeName "openshot-qt.exe"

[Setup]
; Different AppId than the official installer, so this build installs
; alongside (not over) a standard OpenShot installation.
AppId={{B2CDA13C-4EFB-4EB4-B95C-721648ABE59E}
AppName={#MyAppName}
AppVersion={#VERSION}
VersionInfoVersion={#VERSION}
AppPublisher=CyberNerdIT (unofficial build of OpenShot Video Editor)
AppPublisherURL=https://github.com/CyberNerdIT/OpenShot
AppSupportURL=https://github.com/CyberNerdIT/OpenShot/issues
DefaultDirName={autopf}\OpenShot Video Editor OCCLUDE
DisableProgramGroupPage=yes
LicenseFile=..\COPYING
OutputBaseFilename=OpenShot-OCCLUDE-v{#VERSION}-x86_64
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
Compression=lzma
SolidCompression=yes
WizardSmallImageFile=installer-logo.bmp
SetupIconFile=..\xdg\openshot-qt.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SOURCE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
