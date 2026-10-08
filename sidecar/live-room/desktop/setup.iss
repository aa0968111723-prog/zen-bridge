#ifndef PayloadDir
  #error PayloadDir must point to the prepared offline App
#endif
#ifndef AppVersion
  #define AppVersion "0.3.1"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef PythonVersion
  #define PythonVersion "3.12.10"
#endif
[Setup]
AppId={{68770E3F-50EE-493F-8A23-6A82C5FFDA69}
AppName=禪譯聽眾房
AppVersion={#AppVersion}
AppPublisher=Breeze Live Room
AppPublisherURL=https://github.com/aa0968111723-prog/breeze-live-room
DefaultDirName={localappdata}\Programs\Breeze Live Room
DefaultGroupName=禪譯聽眾房
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=Breeze-Live-Room-Setup
; The model and wheels are already compressed. zip keeps the one-click build practical.
Compression=zip
SolidCompression=no
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
UsePreviousLanguage=no
UsePreviousGroup=no
ShowLanguageDialog=no
UninstallDisplayIcon={app}\Breeze.exe
CloseApplications=yes
RestartApplications=no
AppMutex=Local\BreezeLiveRoomApp
SetupLogging=yes

[Languages]
Name: "chinesetraditional"; MessagesFile: "Chinese.isl"

[Messages]
SetupAppTitle=安裝
SetupWindowTitle=安裝禪譯聽眾房
WelcomeLabel1=安裝禪譯聽眾房
WelcomeLabel2=這會在這台電腦裝上本機中文字幕。%n%n不用另外裝 Python，也不用系統管理員。你不用選資料夾。%n%n完成後，桌面會出現「禪譯聽眾房」。已經裝過的設定與字幕會留著。
ClickNext=按「下一步」繼續。
ButtonNext=下一步(&N) >
ButtonInstall=安裝(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消
ButtonBack=< 上一步(&B)
ReadyLabel1=可以開始安裝了。
ReadyLabel2a=按「安裝」。大約要一分鐘，請不要關閉這個視窗。%n%n會裝在你的個人程式資料夾。已有的設定與字幕不會被清掉。
WizardInstalling=正在安裝
InstallingLabel=正在安裝禪譯聽眾房，請稍候。
StatusCreateDirs=正在準備資料夾...
StatusExtractFiles=正在複製程式與辨識模型...
StatusCreateIcons=正在建立桌面捷徑...
StatusRunProgram=即將完成...
FinishedHeadingLabel=安裝完成
FinishedLabel=桌面已有「禪譯聽眾房」。%n%n按「完成」就會開啟。第一次準備辨識大約要半分鐘，請先不要關閉程式視窗。
FinishedLabelNoIcons=安裝完成。可以從開始功能表開啟「禪譯聽眾房」。
ClickFinish=按「完成」開啟。
LaunchProgram=立刻開啟禪譯聽眾房
ConfirmUninstall=確定要移除禪譯聽眾房嗎？設定與字幕會留在電腦上。
ExitSetupMessage=安裝還沒完成。確定要離開嗎？
SetupAborted=安裝未完成。可以稍後再執行一次安裝程式。

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pyc,__pycache__\*,.env,data\*,logs\*,tmp\*,.venv\*,.updates\*,models\*"
; Quantized weights hardly compress; copy them directly to keep builds fast.
Source: "{#PayloadDir}\models\*"; DestDir: "{app}\models"; Flags: ignoreversion nocompression
Source: "{#PayloadDir}\.env.example"; DestDir: "{app}"; DestName: ".env"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\logs"; Flags: uninsneveruninstall
Name: "{app}\tmp"

[InstallDelete]
; Replace only the App-managed dependency tree. User settings and captions
; live outside .python and are retained during reinstall and uninstall.
Type: filesandordirs; Name: "{app}\.python\{#PythonVersion}\tools\Lib\site-packages"
Type: files; Name: "{autodesktop}\Breeze Live Room.lnk"
Type: files; Name: "{userprograms}\Breeze Live Room\Breeze Live Room.lnk"
Type: files; Name: "{userprograms}\Breeze Live Room\Uninstall Breeze Live Room.lnk"
Type: dirifempty; Name: "{userprograms}\Breeze Live Room"

[Icons]
Name: "{autodesktop}\禪譯聽眾房"; Filename: "{app}\Breeze.exe"; WorkingDir: "{app}"
Name: "{group}\禪譯聽眾房"; Filename: "{app}\Breeze.exe"; WorkingDir: "{app}"
Name: "{group}\移除禪譯聽眾房"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Breeze.exe"; Description: "立刻開啟禪譯聽眾房"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2Present: Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Bootstrapper: String;
  VcInstaller: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Bootstrapper := ExpandConstant('{app}\desktop\WebView2Bootstrapper.exe');
  if not WebView2Present then begin
    if (not FileExists(Bootstrapper)) or (not Exec(Bootstrapper, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) then
      RaiseException('Microsoft WebView2 安裝未完成，請重新執行安裝程式。');
    if (ResultCode <> 0) and (ResultCode <> 3010) then
      RaiseException('Microsoft WebView2 安裝失敗，請確認網路後重新安裝。');
  end;
  VcInstaller := ExpandConstant('{app}\desktop\vc_redist.x64.exe');
  if not FileExists(ExpandConstant('{sys}\msvcp140.dll')) then begin
    if (not FileExists(VcInstaller)) or (not Exec(VcInstaller, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) then
      RaiseException('Microsoft Visual C++ 安裝未完成，請重新執行安裝程式。');
    if (ResultCode <> 0) and (ResultCode <> 3010) and (ResultCode <> 1638) then
      RaiseException('Microsoft Visual C++ 安裝失敗；可能需要管理員協助。');
  end;
end;
