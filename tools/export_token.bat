@echo off
chcp 65001 >nul
setlocal
set "ELECTRON_RUN_AS_NODE=1"
set "WB_EXE=%ProgramFiles%\WorkBuddy\WorkBuddy.exe"
if not exist "%WB_EXE%" set "WB_EXE=%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"
if not exist "%WB_EXE%" (
  echo [ERROR] WorkBuddy.exe not found. Edit WB_EXE in this file.
  exit /b 1
)
"%WB_EXE%" "%~dp0export_token.cjs"
echo.
echo Done. Paste data\refresh_token into the MoviePilot plugin settings page.
pause
