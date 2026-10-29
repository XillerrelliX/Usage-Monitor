@echo off
setlocal
title Usage Monitor
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" %*
set "usageMonitorExit=%errorlevel%"
if "%usageMonitorExit%"=="1" (
  echo.
  echo Usage Monitor could not start. The error above explains how to recover.
  if "%~1"=="" pause
)
exit /b %usageMonitorExit%
