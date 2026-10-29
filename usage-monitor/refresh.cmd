@echo off
setlocal
if exist "%~dp0bundle-manifest.json" (
  "%~dp0Start-Usage-Monitor.cmd" %*
  exit /b
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
exit /b %ERRORLEVEL%
