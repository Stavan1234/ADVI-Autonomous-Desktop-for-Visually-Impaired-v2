$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "ADVI Windows setup" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"
Write-Host ""

if ($PSVersionTable.PSEdition -notin @("Desktop", "Core")) {
    throw "PowerShell is required."
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    throw "Python Launcher 'py' was not found. Install Python 3.13 x64 from python.org and retry."
}

$pyVersion = (& py -3.13 --version 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.13 was not found. Install Python 3.13 x64 and retry."
}
Write-Host "$pyVersion" -ForegroundColor Green

$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv..." -ForegroundColor Yellow
    & py -3.13 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}

Write-Host "Updating pip..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

Write-Host "Installing ADVI runtime + development dependencies..." -ForegroundColor Yellow
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "ADVI dependency installation failed." }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Add at least one provider API key." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists; leaving it untouched." -ForegroundColor Green
}

$assetChecks = @(
    "assets\piper\piper.exe",
    "assets\models\en_US-lessac-medium.onnx",
    "assets\models\en_US-lessac-medium.onnx.json"
)
$allPiperAssetsPresent = $true
foreach ($asset in $assetChecks) {
    if (-not (Test-Path $asset)) {
        $allPiperAssetsPresent = $false
        Write-Host "Piper asset not present: $asset" -ForegroundColor Yellow
    }
}
if ($allPiperAssetsPresent) {
    Write-Host "Bundled Piper assets: OK" -ForegroundColor Green
} else {
    Write-Host "Bundled Piper assets: NOT COMPLETE. ADVI will use Windows built-in speech output when available." -ForegroundColor Yellow
}

$tesseract = Get-Command tesseract -ErrorAction SilentlyContinue
$tesseractPaths = @("$env:ProgramFiles\Tesseract-OCR\tesseract.exe", "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe", "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe")
if ($tesseract -or ($tesseractPaths | Where-Object { $_ -and (Test-Path $_) })) {
    Write-Host "Tesseract OCR: FOUND" -ForegroundColor Green
} else {
    Write-Host "Tesseract OCR: NOT FOUND. Telegram messages and screen reading need it:" -ForegroundColor Yellow
    Write-Host "    winget install UB-Mannheim.TesseractOCR" -ForegroundColor Yellow
}

$chromePaths = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
if ($chromePaths | Where-Object { $_ -and (Test-Path $_) }) {
    Write-Host "Google Chrome: FOUND" -ForegroundColor Green
} else {
    Write-Host "Google Chrome: NOT FOUND. Websites, YouTube and David Gmail need it:" -ForegroundColor Yellow
    Write-Host "    winget install Google.Chrome" -ForegroundColor Yellow
}

Write-Host "Running local source validation..." -ForegroundColor Yellow
& $venvPython -m compileall -q src
if ($LASTEXITCODE -ne 0) { throw "Source compilation failed." }

Write-Host "Running the current ADVI regression suite..." -ForegroundColor Yellow
# test_groq_smoke.py calls the real Groq API; it is skipped here so a missing or
# not-yet-added key cannot fail the installation.
& $venvPython -m pytest -q --deselect tests/current/test_groq_smoke.py
if ($LASTEXITCODE -ne 0) { throw "ADVI regression tests failed. Fix the failing test(s) before using the installation." }

Write-Host "" 
Write-Host "ADVI installation and source validation complete." -ForegroundColor Green
Write-Host "Edit .env, then run .\run_advi.bat" -ForegroundColor Cyan
Write-Host "See README.md for things to try." -ForegroundColor Cyan
