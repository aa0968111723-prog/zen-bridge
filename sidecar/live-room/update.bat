@echo off
setlocal
set PSModulePath=
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
set "result=%errorlevel%"
if not defined BREEZE_NONINTERACTIVE pause
exit /b %result%
