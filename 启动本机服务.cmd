@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\restart-local.ps1"
set "service_exit=%errorlevel%"
if not "%service_exit%"=="0" echo Service startup failed. See the error above.
if /i not "%~1"=="--no-pause" pause
exit /b %service_exit%
