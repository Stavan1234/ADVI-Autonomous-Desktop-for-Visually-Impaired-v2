$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "ADVI virtual environment not found at $Python"
    exit 1
}
& $Python (Join-Path $PSScriptRoot "smoke_windows.py")
exit $LASTEXITCODE
