@echo off
if exist "%~dp0bundle-manifest.json" (
  "%~dp0Start-Usage-Monitor.cmd" %*
  exit /b
)
call "%~dp0refresh.cmd" --setup --offline %*
exit /b %ERRORLEVEL%
