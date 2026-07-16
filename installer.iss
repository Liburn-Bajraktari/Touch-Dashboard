#define MyAppName        "Touch Dashboard"
#define MyAppVersion     "0.2.5"
#define MyAppPublisher   "Touch Dashboard"
#define MyAppURL         "https://codeberg.org/liburnb/Touch-Dashboard"
#define MyAppExeName     "TouchDashboard.exe"
#define MyAppUserModelID "TouchDashboard.TouchDashboard"
#define MyAppMutex       "TouchDashboardSingleInstanceMutex"

; ── VC++ Redist ────────────────────────────────────────────────────────────────
; PyQt6/WebEngine and sounddevice DLLs require the MSVC 2015-2022 x64 runtime.
#define VCRedistKey  "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
#define VCRedistURL  "https://aka.ms/vs/17/release/vc_redist.x64.exe"

[Setup]
AppId={{9B76460B-B955-46A8-84E1-7D726F32F65B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\Touch Dashboard
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=installers
OutputBaseFilename=TouchDashboardSetup
SetupIconFile=static\favicon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex={#MyAppMutex}
CloseApplications=yes
CloseApplicationsFilter=*{#MyAppExeName}
RestartIfNeededByRun=no
RestartApplications=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
UsedUserAreasWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup";     Description: "Start Touch Dashboard automatically at login"; GroupDescription: "Startup options:"; Flags: unchecked

[Files]
Source: "dist\TouchDashboard\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\TouchDashboard\*";               DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";       Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]

procedure KillRunningApp;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Returns True if the MSVC 2015-2022 x64 runtime is already installed.
// Checks three registry paths because different VS versions write different keys.
function VCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result := False;

  // Path 1: standard 64-bit hive (VC++ 2017-2022)
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
    if Installed = 1 then begin Log('VC++ found via VisualStudio\14.0 64-bit'); Result := True; Exit; end;

  // Path 2: 32-bit WOW64 view (some 2015 installers)
  if RegQueryDWordValue(HKLM32, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
    if Installed = 1 then begin Log('VC++ found via VisualStudio\14.0 WOW64'); Result := True; Exit; end;

  // Path 3: VC\14 shorthand (seen on some 2022 installs)
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VC\14\x64', 'Installed', Installed) then
    if Installed = 1 then begin Log('VC++ found via VC\14\x64'); Result := True; Exit; end;
end;

// Download a URL to a local file using WinHTTP COM (no plugin required).
function DownloadFile(const URL, Dest: String): Boolean;
var
  WinHttp: Variant;
  Stream:  Variant;
begin
  Result := False;
  try
    WinHttp := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    WinHttp.Open('GET', URL, False);
    WinHttp.SetOption(6, True);  // EnableRedirects
    WinHttp.Send('');
    if WinHttp.Status <> 200 then begin
      Log('HTTP download status: ' + IntToStr(WinHttp.Status));
      Exit;
    end;
    Stream := CreateOleObject('ADODB.Stream');
    Stream.Type_ := 1;   // adTypeBinary
    Stream.Mode  := 3;   // adModeReadWrite
    Stream.Open;
    Stream.Write(WinHttp.ResponseBody);
    Stream.SaveToFile(Dest, 2);  // adSaveCreateOverWrite
    Stream.Close;
    Result := True;
  except
    Log('DownloadFile exception: ' + GetExceptionMessage);
  end;
end;

// Download and silently install the VC++ 2015-2022 x64 Redistributable.
function InstallVCRedist: Boolean;
var
  TempFile:   String;
  ResultCode: Integer;
begin
  Result   := False;
  TempFile := ExpandConstant('{tmp}\vc_redist.x64.exe');
  Log('Downloading VC++ Redist from Microsoft...');
  if not DownloadFile('{#VCRedistURL}', TempFile) then begin
    MsgBox(
      'Touch Dashboard requires the Microsoft Visual C++ 2015-2022 Redistributable (x64).' + #13#10 + #13#10 +
      'Automatic download failed. Please install it manually and run setup again.' + #13#10 +
      'Download: {#VCRedistURL}',
      mbError, MB_OK);
    Exit;
  end;
  Log('Running VC++ Redist installer silently...');
  if not Exec(TempFile, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then begin
    MsgBox('Failed to launch the Visual C++ installer. Please install it manually: {#VCRedistURL}', mbError, MB_OK);
    Exit;
  end;
  // 0=success  3010=success+reboot  1638=already installed
  Result := (ResultCode = 0) or (ResultCode = 3010) or (ResultCode = 1638);
  if not Result then
    Log('VC++ Redist exit code: ' + IntToStr(ResultCode));
end;

// Called by Inno Setup before any files are extracted.
// Return '' to proceed; non-empty string to abort with that message.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not VCRedistInstalled then begin
    Log('VC++ 2015-2022 x64 not found — installing...');
    if not InstallVCRedist then begin
      Result :=
        'Setup could not install the required Microsoft Visual C++ 2015-2022 ' +
        'Redistributable (x64).' + #13#10 + #13#10 +
        'Please install it manually from:' + #13#10 +
        '  {#VCRedistURL}' + #13#10 + #13#10 +
        'Then re-run this setup.';
      Exit;
    end;
  end else
    Log('VC++ 2015-2022 x64 already installed — skipping.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    KillRunningApp;
    Sleep(500);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    KillRunningApp;
    Sleep(500);
  end else if CurUninstallStep = usPostUninstall then begin
    if MsgBox(
      'Do you want to completely remove your saved configurations, cached music, and downloaded sounds?' + #13#13 +
      'Click Yes for a full uninstall, or No to keep your settings for a future reinstall.',
      mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{userappdata}\touch-dashboard'), True, True, True);
    end;
  end;
end;
