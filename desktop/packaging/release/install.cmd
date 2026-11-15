@echo off
rem Install Meridian Desktop for this Windows user. No administrator rights are needed.
rem Runs MeridianDesktop.exe --install --desktop from the folder this script is in.
setlocal
cd /d "%~dp0"
"%~dp0MeridianDesktop.exe" --install --desktop
if errorlevel 1 (
  echo.
  echo Install did not finish. Read the message, close Meridian Desktop if it is running, and try again.
  pause
  exit /b 1
)
echo.
echo Meridian Desktop is installed. Start it from the Start Menu or the desktop shortcut.
pause
exit /b 0
