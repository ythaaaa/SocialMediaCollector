#define MyAppName "Social Media Collector"
#define MyAppVersion "1.0.0"
#define MyAppPublisher ""
#define MyAppExeName "SocialMediaCollector.exe"

[Setup]
AppId={{8FA84253-509A-47BF-8A33-FB2C4357EFC5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
UninstallDisplayName={#MyAppName}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\SocialMediaCollector
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\release
OutputBaseFilename=SocialMediaCollector-Setup-v1.0.0
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoDescription={#MyAppName}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoVersion=1.0.0.0
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
Uninstallable=yes
RestartApplications=no
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "..\dist\SocialMediaCollector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\third_party\ffmpeg\LICENSE.txt"; DestDir: "{app}\licenses\ffmpeg"; Flags: ignoreversion
Source: "..\third_party\ffmpeg\NOTICE.txt"; DestDir: "{app}\licenses\ffmpeg"; Flags: ignoreversion
Source: "..\third_party\ffmpeg\SOURCE_INFO.txt"; DestDir: "{app}\licenses\ffmpeg"; Flags: ignoreversion
Source: "..\third_party\ffmpeg\BUILD_INFO.txt"; DestDir: "{app}\licenses\ffmpeg"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
