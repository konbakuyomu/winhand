; winhand installer: a standard Windows setup wizard around the Velopack app layout.
;
; The payload is Velopack's portable layout (Update.exe, current\, winhand.exe launcher, .portable),
; so the app keeps updating itself in place wherever the user installs it. Inno Setup owns what a
; person expects from an installer: wizard, folder choice, shortcuts, start with Windows, entry in
; "Installed apps", uninstall; and it migrates a previous Velopack Setup install.
;
; Built by desktop/scripts/Build-Windows.ps1:
;   ISCC /DAppVersion=0.3.4 /DPayloadDir=... /DOutputDir=... [/DSigned "/Swinhandsign=..."] winhand.iss

#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir must point to the extracted Velopack portable package
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#define Repo "https://github.com/konbakuyomu/winhand"
#define Images(prefix) prefix + "-100.bmp," + prefix + "-125.bmp," + prefix + "-150.bmp," + prefix + "-175.bmp," + prefix + "-200.bmp," + prefix + "-225.bmp," + prefix + "-250.bmp"

[Setup]
AppId={{BD773DCA-E6D2-427C-92E1-5900C6FF13F1}
AppName=winhand
AppVersion={#AppVersion}
AppVerName=winhand {#AppVersion}
AppPublisher=konbakuyomu
AppPublisherURL={#Repo}
AppSupportURL={#Repo}/issues
AppUpdatesURL={#Repo}/releases
AppCopyright=Copyright (c) 2026 konbakuyomu
VersionInfoVersion={#AppVersion}
VersionInfoProductName=winhand
VersionInfoDescription=winhand Setup
; per user: the app updates itself, so the install folder must stay writable without elevation
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\winhand
DisableDirPage=no
DisableWelcomePage=no
DefaultGroupName=winhand
DisableProgramGroupPage=no
AllowNoIcons=yes
LicenseFile=..\..\..\LICENSE
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
WizardStyle=modern
WizardSizePercent=110
WizardImageFile={#Images("wizard\large")}
WizardSmallImageFile={#Images("wizard\small")}
SetupIconFile=..\..\windows\Assets\winhand.ico
UninstallDisplayName=winhand
UninstallDisplayIcon={app}\current\WinhandDesktop.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
CloseApplications=no
RestartApplications=no
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=winhand-{#AppVersion}-Setup
#ifdef Signed
SignTool=winhandsign
SignedUninstaller=yes
#endif

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"; InfoBeforeFile: "info-zh.txt"
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "info-en.txt"

[CustomMessages]
zh.AutostartTask=登录 Windows 后自动启动（在通知区域运行，并自动连接中转）
zh.StartupGroup=启动：
zh.AppRunning=winhand 正在运行。安装程序需要先关闭它，Claude 与这台电脑的连接会暂时断开，装好后会重新连接。%n%n点击“确定”关闭 winhand 并继续。
zh.AppStillRunning=无法关闭正在运行的 winhand。请右键托盘图标选择“退出 winhand”，然后重试。
zh.MigrationMemo=迁移：
zh.MigrationDetail=先移除旧版 winhand（%1），配置、令牌和活动记录保留
zh.MigrationFailed=移除旧版 winhand 失败（代码 %1）。请在 Windows“设置 → 应用 → 已安装的应用”里卸载旧版后重试。
zh.NeedsWritableDir=这个文件夹需要管理员权限才能写入。%n%nwinhand 会在后台自动更新自己，必须装在当前用户能写入的位置，例如 D:\Apps\winhand 或默认位置。
zh.RemoveUserData=是否同时删除 winhand 的配置、日志和活动记录？%n%n%1%n%n其中包含中转地址和设备令牌。选择“否”则保留，重新安装后可以直接使用。
en.AutostartTask=Start with Windows (runs in the notification area and connects to the relay)
en.StartupGroup=Startup:
en.AppRunning=winhand is running. Setup needs to close it; Claude's connection to this computer drops until the new version starts.%n%nClick OK to close winhand and continue.
en.AppStillRunning=winhand could not be closed. Right-click the tray icon, choose "Exit winhand" and try again.
en.MigrationMemo=Migration:
en.MigrationDetail=Remove the previous winhand install (%1); settings, token and activity are kept
en.MigrationFailed=Removing the previous winhand install failed (code %1). Uninstall it in Windows Settings, then try again.
en.NeedsWritableDir=This folder needs administrator rights.%n%nwinhand updates itself in the background, so it must be installed where the current user can write, for example D:\Apps\winhand or the default location.
en.RemoveUserData=Also delete winhand's settings, logs and activity history?%n%n%1%n%nThey include the relay address and device token. Choose No to keep them for a later reinstall.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutostartTask}"; GroupDescription: "{cm:StartupGroup}"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\winhand"; Filename: "{app}\winhand.exe"; IconFilename: "{app}\current\WinhandDesktop.exe"
Name: "{group}\{cm:UninstallProgram,winhand}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\winhand"; Filename: "{app}\winhand.exe"; IconFilename: "{app}\current\WinhandDesktop.exe"; Tasks: desktopicon

[Registry]
; the same command the app's own "start with Windows" switch writes, so both stay in agreement
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "winhand"; \
  ValueData: """{app}\current\WinhandDesktop.exe"" --background"; Tasks: autostart

[Run]
; started through Explorer: a child of Setup would inherit its RedirectionGuard mitigation and
; could not follow user-created junctions (scoop, mise ...) in anything it runs
Filename: "{win}\explorer.exe"; Parameters: """{app}\winhand.exe"""; Description: "{cm:LaunchProgram,winhand}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; updates replace these after installation, so they are not all in the install log
Type: filesandordirs; Name: "{app}\current"
Type: filesandordirs; Name: "{app}\packages"

[Code]
const
  AppMutex = 'Local\Winhand.Desktop';
  OldUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\winhand';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

{ Close a running winhand after asking. Killing the tray app closes the backend's pipe, and the
  backend then stops its sessions and exits by itself. }
function StopRunningApp(): Boolean;
var
  ResultCode, Waited: Integer;
begin
  Result := True;
  if not CheckForMutexes(AppMutex) then
    Exit;
  if SuppressibleMsgBox(CustomMessage('AppRunning'), mbConfirmation, MB_OKCANCEL, IDOK) <> IDOK then
  begin
    Result := False;
    Exit;
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM WinhandDesktop.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while CheckForMutexes(AppMutex) and (Waited < 15000) do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  Sleep(2500);
  Result := not CheckForMutexes(AppMutex);
end;

function OldInstallLocation(): String;
begin
  if not RegQueryStringValue(HKCU, OldUninstallKey, 'InstallLocation', Result) then
    Result := '';
end;

function StartsWith(const Value, Prefix: String): Boolean;
begin
  Result := Pos(Lowercase(AddBackslash(Prefix)), Lowercase(AddBackslash(Value))) = 1;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    if StartsWith(Dir, ExpandConstant('{commonpf64}')) or StartsWith(Dir, ExpandConstant('{commonpf32}')) or
       StartsWith(Dir, ExpandConstant('{win}')) then
    begin
      SuppressibleMsgBox(CustomMessage('NeedsWritableDir'), mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine;
  if MemoGroupInfo <> '' then
    Result := Result + NewLine + MemoGroupInfo + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo + NewLine;
  if OldInstallLocation() <> '' then
    Result := Result + NewLine + CustomMessage('MigrationMemo') + NewLine + Space +
      FmtMessage(CustomMessage('MigrationDetail'), [OldInstallLocation()]) + NewLine;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Command: String;
  ResultCode: Integer;
begin
  Result := '';
  if not StopRunningApp() then
  begin
    Result := CustomMessage('AppStillRunning');
    Exit;
  end;
  { a previous install made by Velopack's own Setup: remove it, user data stays in ~\.winhand }
  if RegQueryStringValue(HKCU, OldUninstallKey, 'QuietUninstallString', Command) then
  begin
    if not Exec(ExpandConstant('{cmd}'), '/C "' + Command + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or
       (ResultCode <> 0) then
      Result := FmtMessage(CustomMessage('MigrationFailed'), [IntToStr(ResultCode)]);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { unticking "start with Windows" also clears an entry the app or an older install made }
  if (CurStep = ssPostInstall) and not WizardIsTaskSelected('autostart') then
    RegDeleteValue(HKCU, RunKey, 'winhand');
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopRunningApp();
  if not Result then
    SuppressibleMsgBox(CustomMessage('AppStillRunning'), mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value, Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    if RegQueryStringValue(HKCU, RunKey, 'winhand', Value) and
       (Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Value)) > 0) then
      RegDeleteValue(HKCU, RunKey, 'winhand');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{%USERPROFILE}\.winhand');
    if DirExists(Data) and
       (SuppressibleMsgBox(FmtMessage(CustomMessage('RemoveUserData'), [Data]), mbConfirmation,
          MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
      DelTree(Data, True, True, True);
  end;
end;
