@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 請先執行 install.bat。
  if not defined BREEZE_NONINTERACTIVE pause
  exit /b 1
)
echo 請先關閉字幕服務，現在檢查實際模型與辨識速度。
.venv\Scripts\python.exe scripts\verify_runtime.py %*
set RESULT=%errorlevel%
if not defined BREEZE_NONINTERACTIVE pause
exit /b %RESULT%
