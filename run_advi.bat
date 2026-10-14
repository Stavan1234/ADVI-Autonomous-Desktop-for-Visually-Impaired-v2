@echo off
setlocal
if not exist ".venv\Scripts\python.exe" (
  echo ADVI virtual environment not found. Run SETUP_ADVI.bat first.
  exit /b 1
)
".venv\Scripts\python.exe" -m advi.app %*
endlocal
