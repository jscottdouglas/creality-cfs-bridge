; installer/CrealityCFSBridge.iss -- compiled with ISCC by build_installer.ps1,
; which passes /DVersion, /DOrcaUrl, /DOrcaSha and /DOrcaAsset from
; installer/version.txt and installer/orca_release.json. The defaults below
; exist only so the script still compiles on its own, for a syntax check.
;
; It packages dist\CrealityCFSBridge\ exactly as installer\build_package.ps1
; leaves it: cfsbridge.exe (the bridge, PyInstaller one folder), fluidd\,
; and the renamed WinSW binary CrealityCFSBridge.exe with its
; CrealityCFSBridge.xml service definition. The installer registers that
; service, writes the slicer preset, opens the setup page so the printer
; address can be entered, and can chain the slicer's own installer.
;
; Run as: compile from the project root so that ..\dist and ..\LICENSE resolve.

; ArchitecturesInstallIn64BitMode=x64compatible below was introduced in 6.3,
; and an older compiler treats the unknown value as an error only at the point
; it reaches it, which reads as a typo rather than as a version problem.
#if VER < EncodeVer(6,3,0)
  #error "Inno Setup 6.3 or newer is required: x64compatible needs it."
#endif

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef OrcaUrl
  #define OrcaUrl ""
#endif
#ifndef OrcaSha
  #define OrcaSha ""
#endif
#ifndef OrcaAsset
  #define OrcaAsset ""
#endif

[Setup]
; AppId is the identity Windows uses to recognise an upgrade of this product
; and to find it again at uninstall time. It must never change: a new value
; turns every future release into a second, parallel installation that leaves
; the old service registered behind it.
AppId={{1E9A90B9-66D4-4CE3-8EF9-116D6C32FEA4}
AppName=Creality CFS Bridge (unofficial)
AppVersion={#Version}
AppPublisher=jscottdouglas (unofficial, not affiliated with Creality)
AppPublisherURL=https://github.com/jscottdouglas/creality-cfs-bridge
DefaultDirName={autopf}\CrealityCFSBridge
DefaultGroupName=Creality CFS Bridge
OutputBaseFilename=CrealityCFSBridge_Setup_{#Version}
Compression=lzma2
SolidCompression=yes
; The service is registered under HKLM and the files go under Program Files,
; so there is no unelevated install to offer.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
UninstallDisplayName=Creality CFS Bridge (unofficial)
WizardStyle=modern
; So that the released setup exe carries its own version in its file
; properties, which is where anyone checking a downloaded binary looks first.
VersionInfoVersion={#Version}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "orca"; Description: "Also install Unofficial Creality OrcaSlicer with CFS, Camera Support (downloads about 200 MB unless the installer is next to this setup)"; Flags: checkedonce

[Files]
Source: "..\dist\CrealityCFSBridge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; Deliberately redundant: the spec's datas already puts this file at
; installer\write_orca_preset.py inside the packaged folder above, which is
; where `cfsbridge preset` looks for it. Naming it here as well means a
; change to the spec cannot quietly leave the installed copy without the one
; file the preset step needs.
Source: "write_orca_preset.py"; DestDir: "{app}\installer"; Flags: ignoreversion

[Dirs]
; The service runs as NT AUTHORITY\LocalService, and it is the only thing that
; writes here: config.json comes from the setup page, which the service serves,
; and the logs are the service's own. users-modify would not cover a service
; account anyway, so service-modify names it explicitly. Interactive users get
; read and execute, which is enough to open a log or read the saved address,
; and not enough to edit a file the service owns.
Name: "{commonappdata}\CrealityCFSBridge"; Permissions: users-readexec service-modify
Name: "{commonappdata}\CrealityCFSBridge\logs"; Permissions: users-readexec service-modify

[Icons]
Name: "{group}\Uninstall Creality CFS Bridge"; Filename: "{uninstallexe}"

[Run]
; The service is registered and started from CurStepChanged instead, where the
; result of each step can be checked. No --appdata, and runasoriginaluser: the
; preset belongs in the profile of the person who is installing, not in the
; profile the elevated setup process runs under, which on a machine where an
; administrator was prompted for credentials is somebody else's. The command
; falls back to APPDATA, which runasoriginaluser makes the right one. The
; preset does not need the service to be up, so it does not matter whether
; this runs before or after the registration.
Filename: "{app}\cfsbridge.exe"; Parameters: "preset"; StatusMsg: "Writing the slicer preset"; Flags: runhidden waituntilterminated runasoriginaluser
; skipifsilent: a silent install (CI smoke test, scripted deployments) must not open a browser.
Filename: "http://127.0.0.1:7126/setup"; Description: "Open the setup page to enter the printer address"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\CrealityCFSBridge.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated; RunOnceId: "stop"
Filename: "{app}\CrealityCFSBridge.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "uninstall"

[Code]
const OrcaUrl = '{#OrcaUrl}'; OrcaSha = '{#OrcaSha}'; OrcaAsset = '{#OrcaAsset}';
var DownloadPage: TDownloadWizardPage;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

function WinswPath: String;
begin
  Result := ExpandConstant('{app}\CrealityCFSBridge.exe');
end;

{ Run one WinSW command and say whether it worked. Both halves matter: Exec
  returning False means the process never started at all, and a non-zero exit
  code is WinSW saying the command itself failed, which a bare Exec call would
  throw away. Rc is cleared first so that a caller reporting it after a failed
  Exec cannot print a stale code. }
function WinswStep(const Command, Status: String; var Rc: Integer): Boolean;
begin
  Rc := -1;
  WizardForm.StatusLabel.Caption := Status;
  Result := Exec(WinswPath, Command, '', SW_HIDE, ewWaitUntilTerminated, Rc) and (Rc = 0);
end;

{ An upgrade installs over a folder whose service is registered and running,
  which holds cfsbridge.exe open and would leave the old registration behind.
  Stopping and unregistering first fixes both. The exit codes are ignored on
  purpose: "not running" and "not installed" are the normal answers on a
  repair or a reinstall, and WinSW reports them as failures. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Rc: Integer;
begin
  Result := '';
  if FileExists(WinswPath) then begin
    WizardForm.StatusLabel.Caption := 'Stopping the existing Creality CFS Bridge service';
    Exec(WinswPath, 'stop', '', SW_HIDE, ewWaitUntilTerminated, Rc);
    Exec(WinswPath, 'uninstall', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  end;
end;

{ The slicer installer beside this setup, when there is one. A release
  download that already carries both files should not fetch 200 MB again, and
  an offline install should still work. }
function LocalOrca: String;
begin
  Result := ExpandConstant('{src}\') + OrcaAsset;
  if not FileExists(Result) then Result := '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('orca') and (LocalOrca = '') then begin
    DownloadPage.Clear;
    try
      try
        { Add and Show are inside the try as well: Add raises on an expected
          hash it cannot parse, and that is a build mistake the user can do
          nothing about, so it deserves the same plain message as a failed
          download rather than a raw internal error. Inno checks the hash
          itself during Download, so a replaced or truncated asset never
          reaches the Exec below either. }
        DownloadPage.Add(OrcaUrl, OrcaAsset, OrcaSha);
        DownloadPage.Show;
        DownloadPage.Download;
      except
        { A failed slicer download is not a failed bridge install: the bridge
          is the product here and the slicer is an extra, so setup says so and
          carries on rather than rolling back. }
        SuppressibleMsgBox('The slicer installer could not be downloaded or its checksum did not match. The bridge will still be installed; get the slicer from the release page.', mbError, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure RegisterService;
var Rc: Integer; Failed: String;
begin
  Failed := '';
  if not WinswStep('install', 'Registering the service', Rc) then
    Failed := 'register the service'
  else if not WinswStep('start', 'Starting the service', Rc) then
    Failed := 'start the service';
  if Failed <> '' then
    SuppressibleMsgBox('Setup could not ' + Failed + ' (exit code ' + IntToStr(Rc)
      + '). The usual reason is that port 7126 is already taken by another copy'
      + ' of the bridge, such as a scheduled task or a manual run left over from'
      + ' an earlier setup: stop that first, then run this installer again. The'
      + ' service writes its own log to '
      + ExpandConstant('{commonappdata}\CrealityCFSBridge\logs') + '.',
      mbError, MB_OK, IDOK);
end;

procedure InstallSlicer;
var Path: String; Rc: Integer;
begin
  Path := LocalOrca;
  if Path = '' then Path := ExpandConstant('{tmp}\') + OrcaAsset;
  if not FileExists(Path) then begin
    SuppressibleMsgBox('The slicer installer was not found; the bridge is installed. Download the slicer from the release page.', mbInformation, MB_OK, IDOK);
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing the slicer';
  { /S is the silent switch the upstream NSIS installer honours. }
  if not Exec(Path, '/S', '', SW_SHOW, ewWaitUntilTerminated, Rc) then
    SuppressibleMsgBox('The slicer installer could not be started; the bridge is installed. Run ' + OrcaAsset + ' yourself to add the slicer.', mbError, MB_OK, IDOK)
  else if Rc <> 0 then
    SuppressibleMsgBox('The slicer installer exited with code ' + IntToStr(Rc) + ', so it may not have finished; the bridge is installed. Run ' + OrcaAsset + ' again if the slicer is missing.', mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then Exit;
  RegisterService;
  if WizardIsTaskSelected('orca') then InstallSlicer;
end;
