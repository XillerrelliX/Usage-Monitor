@echo off
setlocal
title Install Usage Monitor
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "usageMonitorInstallExit=%errorlevel%"
if "%usageMonitorInstallExit%"=="1" (
  echo.
  echo Usage Monitor installation failed. Read the error above.
  if "%~1"=="" pause
)
exit /b %usageMonitorInstallExit%
