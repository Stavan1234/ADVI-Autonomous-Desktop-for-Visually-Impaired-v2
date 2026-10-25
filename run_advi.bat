@echo off
setlocal
rem Run from this file's folder, so a shortcut or another working directory still finds .venv and .env.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ADVI virtual environment not found. Run SETUP_ADVI.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m advi.app %*
endlocal
