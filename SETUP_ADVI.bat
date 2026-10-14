@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo ADVI - Windows Setup
echo ========================================
echo.

echo This will create the .venv and install ADVI.
echo You will still need to add your LLM API key in .env.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
if errorlevel 1 (
  echo.
  echo SETUP FAILED. Read the error above.
  pause
  exit /b 1
)

echo.
echo ========================================
echo SETUP COMPLETE
echo ========================================
echo.
echo Next: edit .env, then double-click run_advi.bat
pause
endlocal
