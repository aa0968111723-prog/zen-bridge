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
set BREEZE_OPEN_BROWSER=1
.venv\Scripts\python.exe -m app.run
if errorlevel 1 (
  echo 啟動未完成。請依上方檢查結果修正，或執行 doctor.bat。
  if not defined BREEZE_NONINTERACTIVE pause
  exit /b 1
)
