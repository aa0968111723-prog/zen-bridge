@echo off
call "%~dp0update.bat" --rollback
exit /b %errorlevel%
