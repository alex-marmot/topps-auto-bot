#define MyAppName "Topps Auto Bot"
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#define MyAppPublisher "Independent project"
#define MyAppExeName "ToppsAutoBot.exe"

[Setup]
AppId={{5A3C7E5E-6B8E-4F42-9A0A-5A3C7E5E6B8E}}
AppName={#MyAppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ToppsAutoBot
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
OutputDir=..\installer-output
OutputBaseFilename=ToppsAutoBot-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=no
AppMutex=Local\ToppsAutoBot.Application
SetupMutex=Local\ToppsAutoBot.Setup
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Files]
Source: "..\dist\ToppsAutoBot\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Locator, Wmi, Processes: Variant;
begin
  Result := '';
  try
    { Older builds did not register the application mutex. }
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Wmi := Locator.ConnectServer('.', 'root\CIMV2');
    Processes := Wmi.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = "ToppsAutoBot.exe"');
    if (Processes.Count > 0) or CheckForMutexes('Local\ToppsAutoBot.Application') then
      Result := '请在 Topps Auto Bot 中停止任务并正常退出应用，然后重试安装。';
  except
    Result := '无法确认 Topps Auto Bot 已退出，安装已停止。';
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\ToppsAutoBot');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否同时删除本程序的本地配置（含账号和信用卡资料）、登录状态、驱动及全部加购和付款记录？' + #13#10 +
                '默认保留。选择“否”只删除程序文件。', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
