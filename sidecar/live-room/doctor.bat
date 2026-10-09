@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 請先雙擊 install.bat。
  if not defined BREEZE_NONINTERACTIVE pause
  exit /b 1
)
.venv\Scripts\python.exe -m app.doctor --verify-model
set RESULT=%errorlevel%
if not defined BREEZE_NONINTERACTIVE pause
exit /b %RESULT%
