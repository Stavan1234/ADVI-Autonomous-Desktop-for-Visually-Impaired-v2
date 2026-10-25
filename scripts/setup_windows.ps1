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

# PyTorch (used for the memory embeddings) installs files up to ~145 characters deep
# inside .venv. Without Windows long-path support, a project folder longer than about
# 100 characters makes pip fail with "No such file or directory" after minutes of
# downloads (seen in a fresh-copy test, 2026-10-25; a GitHub ZIP extracted into
# Downloads gives a ~135-character path). Check up front and explain.
$longPaths = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
if ($ProjectRoot.Length -gt 100 -and $longPaths -ne 1) {
    Write-Host ""
    Write-Host "The ADVI folder path is $($ProjectRoot.Length) characters long:" -ForegroundColor Red
    Write-Host "    $ProjectRoot" -ForegroundColor Red
    Write-Host "Windows limits paths to 260 characters unless long paths are enabled, and the" -ForegroundColor Yellow
    Write-Host "PyTorch package needs a project folder of at most 100 characters. Either:" -ForegroundColor Yellow
    Write-Host "  1. move the folder to a short path such as C:\ADVI and run SETUP_ADVI.bat again, or" -ForegroundColor Yellow
    Write-Host "  2. enable long paths (needs administrator rights), restart, and run setup again:" -ForegroundColor Yellow
    Write-Host '     New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force' -ForegroundColor Yellow
    throw "Project path too long for a Windows installation without long-path support."
}

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

Write-Host "Downloading the offline speech model (about 320 MB, once)..." -ForegroundColor Yellow
# Voice works offline with this local Whisper model; online, Groq's Whisper is used instead.
# A failure here is not fatal: ADVI tries the download again on its first start.
& $venvPython -c "from pathlib import Path; from advi.voice.transcriber import LocalTranscriber, DEFAULT_STT_MODEL; import sys; sys.exit(0 if LocalTranscriber(DEFAULT_STT_MODEL, download_root=Path('assets/whisper')).preload() else 1)"
if ($LASTEXITCODE -eq 0) {
    Write-Host "Offline speech model: OK" -ForegroundColor Green
} else {
    Write-Host "Offline speech model: NOT DOWNLOADED (no internet?). ADVI will retry on first start; typing always works." -ForegroundColor Yellow
}

Write-Host "Downloading the wake-word model (about 75 MB, once)..." -ForegroundColor Yellow
& $venvPython -c "from pathlib import Path; from advi.voice.transcriber import LocalTranscriber; from advi.wake.listener import WAKE_MODEL; import sys; sys.exit(0 if LocalTranscriber(WAKE_MODEL, download_root=Path('assets/whisper')).preload() else 1)"
if ($LASTEXITCODE -eq 0) {
    Write-Host "Wake-word model: OK" -ForegroundColor Green
} else {
    Write-Host "Wake-word model: NOT DOWNLOADED (no internet?). The wake listener retries every minute." -ForegroundColor Yellow
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
# pyproject.toml already adds -q; another -q would hide the "N passed" summary.
& $venvPython -m pytest --deselect tests/current/test_groq_smoke.py
if ($LASTEXITCODE -ne 0) { throw "ADVI regression tests failed. Fix the failing test(s) before using the installation." }

# Record what was installed: after a later "git pull", run_advi.bat installs only what changed.
& $venvPython scripts\ensure_install.py --mark

# The wake word: a background listener started at every Windows sign-in (HKCU Run,
# no administrator rights), so "wake up ADVI" starts ADVI even when it is closed.
# ADVI_WAKE=off in .env (or the environment) skips this.
$wakeSetting = $env:ADVI_WAKE
if (-not $wakeSetting -and (Test-Path ".env")) {
    $line = Select-String -Path ".env" -Pattern '^\s*ADVI_WAKE\s*=\s*(\S*)' | Select-Object -First 1
    if ($line) { $wakeSetting = $line.Matches[0].Groups[1].Value }
}
if ($wakeSetting -and @("0", "off", "false", "no") -contains $wakeSetting.ToLower()) {
    Write-Host "Wake word: off (ADVI_WAKE=off)." -ForegroundColor Yellow
} else {
    & $venvPython -m advi.wake install
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Wake word: ON. Say 'wake up ADVI' to start ADVI. Remove: .venv\Scripts\python.exe -m advi.wake uninstall" -ForegroundColor Green
    } else {
        Write-Host "Wake word: could not be registered; start ADVI with run_advi.bat." -ForegroundColor Yellow
    }
}

Write-Host "" 
Write-Host "ADVI installation and source validation complete." -ForegroundColor Green
Write-Host "Edit .env, then run .\run_advi.bat" -ForegroundColor Cyan
Write-Host "See README.md for things to try." -ForegroundColor Cyan
