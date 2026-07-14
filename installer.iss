#define MyAppName "Touch Dashboard"
#define MyAppVersion "0.2.5"
#define MyAppPublisher "Touch Dashboard"
#define MyAppExeName "TouchDashboard.exe"
#define MyAppUserModelID "TouchDashboard.TouchDashboard"

[Setup]
AppId={{9B76460B-B955-46A8-84E1-7D726F32F65B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Touch Dashboard
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=installers
OutputBaseFilename=TouchDashboardSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=static\favicon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\TouchDashboard.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  IsUpdate: Boolean;

procedure InitializeWizard;
var
  InstallPath: String;
begin
  // Check if it's already installed by querying the registry (HKLM or HKCU)
  IsUpdate := RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{9B76460B-B955-46A8-84E1-7D726F32F65B}_is1', 'InstallLocation', InstallPath) or
              RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{9B76460B-B955-46A8-84E1-7D726F32F65B}_is1', 'InstallLocation', InstallPath) or
              RegQueryStringValue(HKLM64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{9B76460B-B955-46A8-84E1-7D726F32F65B}_is1', 'InstallLocation', InstallPath) or
              RegQueryStringValue(HKCU64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{9B76460B-B955-46A8-84E1-7D726F32F65B}_is1', 'InstallLocation', InstallPath);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // If it's an update, skip the folder selection page
  if (PageID = wpSelectDir) and IsUpdate then
    Result := True;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpWelcome then
  begin
    if IsUpdate then
    begin
      WizardForm.WelcomeLabel1.Caption := 'Welcome to the Touch Dashboard Update Wizard';
      WizardForm.WelcomeLabel2.Caption := 'This will update Touch Dashboard to version {#MyAppVersion} on your computer.'#13#13'Click Next to continue, or Cancel to exit Setup.';
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    // Forcefully close the application before overwriting files to prevent File Locked errors during updates
    Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(1500); // Give Windows a moment to actually free up Port 5000 so the new app doesn't crash on startup
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Do you want to completely remove your saved configurations, cached music, and downloaded sounds?'#13#13'Click Yes for a full uninstall, or No to keep your settings for the next reinstall.', mbConfirmation, MB_YESNO) = idYes then
    begin
      DelTree(ExpandConstant('{userappdata}\touch-dashboard'), True, True, True);
    end;
  end;
end;
