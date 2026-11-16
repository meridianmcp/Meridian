@echo off
rem Remove Meridian Desktop for this Windows user: shortcuts, the start-with-Windows entry,
rem the Apps and Features entry and the install folder. No administrator rights are needed.
setlocal
"%~dp0MeridianDesktop.exe" --uninstall
if errorlevel 1 (
  echo.
  echo Uninstall did not finish. Close Meridian Desktop if it is running, then try again.
  pause
  exit /b 1
)
echo.
echo Meridian Desktop is removed. Any files that were still in use clear within a few seconds.
pause
exit /b 0
