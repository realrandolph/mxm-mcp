#define ProductName "MXM MCP"
#define ProductVersion "0.1.0"

[Setup]
AppId={{A270810A-2E10-4FE8-B57F-148A313B9D7B}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppPublisher=MXM
DefaultDirName={localappdata}\Programs\mxm-mcp
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\dist
OutputBaseFilename=mxm-mcp-setup-windows-x64
UninstallDisplayName={#ProductName}
UninstallDisplayIcon={app}\mxm-mcp.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ChangesEnvironment=yes

[Tasks]
Name: "opencode"; Description: "Configure OpenCode harness"; Flags: checkedonce
Name: "codex"; Description: "Configure Codex harness"; Flags: checkedonce
Name: "claude"; Description: "Configure Claude Desktop harness"; Flags: checkedonce

[Files]
Source: "..\..\dist\mxm-mcp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\configure_harnesses.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\configure_harnesses.py"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\configure_harnesses.exe"; Parameters: "--target-bin ""{app}\mxm-mcp.exe"" --opencode"; StatusMsg: "Configuring OpenCode harness..."; Flags: runhidden waituntilterminated; Check: WizardIsTaskSelected('opencode')
Filename: "{app}\configure_harnesses.exe"; Parameters: "--target-bin ""{app}\mxm-mcp.exe"" --codex"; StatusMsg: "Configuring Codex harness..."; Flags: runhidden waituntilterminated; Check: WizardIsTaskSelected('codex')
Filename: "{app}\configure_harnesses.exe"; Parameters: "--target-bin ""{app}\mxm-mcp.exe"" --claude"; StatusMsg: "Configuring Claude Desktop harness..."; Flags: runhidden waituntilterminated; Check: WizardIsTaskSelected('claude')

[Code]
const
  EnvironmentKey = 'Environment';
  ProductEnvironmentKey = 'Software\MXM MCP';
  PathOwnershipValue = 'PathEntryAddedByInstaller';

function PathContainsEntry(PathValue, Entry: string): Boolean;
var
  Remaining, Item: string;
  Separator: Integer;
begin
  Result := False;
  Remaining := PathValue + ';';
  while Remaining <> '' do
  begin
    Separator := Pos(';', Remaining);
    Item := Trim(Copy(Remaining, 1, Separator - 1));
    if (Length(Item) >= 2) and (Item[1] = '"') and
       (Item[Length(Item)] = '"') then
      Item := Copy(Item, 2, Length(Item) - 2);
    if CompareText(Item, Entry) = 0 then
    begin
      Result := True;
      Exit;
    end;
    Delete(Remaining, 1, Separator);
  end;
end;

function RemovePathEntry(PathValue, Entry: string): string;
var
  Remaining, Item: string;
  Separator: Integer;
begin
  Result := '';
  Remaining := PathValue + ';';
  while Remaining <> '' do
  begin
    Separator := Pos(';', Remaining);
    Item := Trim(Copy(Remaining, 1, Separator - 1));
    if (Length(Item) >= 2) and (Item[1] = '"') and
       (Item[Length(Item)] = '"') then
      Item := Copy(Item, 2, Length(Item) - 2);
    if (Item <> '') and (CompareText(Item, Entry) <> 0) then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Item;
    end;
    Delete(Remaining, 1, Separator);
  end;
end;

procedure AddApplicationToUserPath;
var
  UserPath, ApplicationPath: string;
  WasAdded: Cardinal;
  MessageResult: DWORD_PTR;
begin
  ApplicationPath := ExpandConstant('{app}');
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', UserPath) then
    UserPath := '';

  WasAdded := 0;
  if PathContainsEntry(UserPath, ApplicationPath) then
  begin
    RegQueryDWordValue(HKEY_CURRENT_USER, ProductEnvironmentKey,
      PathOwnershipValue, WasAdded);
  end
  else
  begin
    if UserPath = '' then
      UserPath := ApplicationPath
    else
      UserPath := UserPath + ';' + ApplicationPath;
    WasAdded := 1;
    RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', UserPath);
  end;

  RegWriteDWordValue(HKEY_CURRENT_USER, ProductEnvironmentKey,
    PathOwnershipValue, WasAdded);
end;

procedure RemoveApplicationFromUserPath;
var
  UserPath, ApplicationPath: string;
  WasAdded: Cardinal;
begin
  ApplicationPath := ExpandConstant('{app}');
  if RegQueryDWordValue(HKEY_CURRENT_USER, ProductEnvironmentKey,
    PathOwnershipValue, WasAdded) and (WasAdded = 1) and
    RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', UserPath) then
  begin
    UserPath := RemovePathEntry(UserPath, ApplicationPath);
    if UserPath = '' then
      RegDeleteValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path')
    else
      RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', UserPath);
  end;
  RegDeleteValue(HKEY_CURRENT_USER, ProductEnvironmentKey, PathOwnershipValue);
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
  MessageResult: Integer;
begin
  Result := True;
  if FileExists('C:\Program Files\MXM\mxm.exe') or
     FileExists('C:\Program Files (x86)\MXM\mxm.exe') or
     FileExists(ExpandConstant('{localappdata}\Programs\MXM\mxm.exe')) then
    Exit;

  MessageResult := MsgBox(
    'MXM DAW was not detected on your system. MXM MCP can create and edit projects without it, but MXM DAW is required for audio rendering and plugin discovery.' + #13#10 + #13#10 +
    'Would you like to open the MXM releases page to download MXM DAW?',
    mbConfirmation, MB_YESNO);
  if MessageResult = IDYES then
    ShellExec('open', 'https://github.com/realrandolph/mxm/releases', '', '',
      SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddApplicationToUserPath;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveApplicationFromUserPath;
end;
