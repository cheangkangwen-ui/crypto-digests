# install.ps1 — Crypto Signal Digest local Windows installer
#
# Run from PowerShell (no admin needed for venv+pip; admin needed only for
# Task Scheduler registration — but `schtasks /create` works user-level too).
#
# Usage:  cd C:\Users\<you>\crypto-signal-digest;  .\install.ps1
#
# Idempotent: re-running re-creates venv if missing, refreshes deps, prompts
# only for missing secrets, and reinstalls scheduled tasks.

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$EnvFile = Join-Path $ProjectDir ".env"
$CookiesFile = Join-Path $ProjectDir "x_cookies.json"
$RunScript = Join-Path $ProjectDir "run.ps1"

Write-Host "==> Crypto Signal Digest installer"
Write-Host "    Project dir: $ProjectDir"
Write-Host ""

# --- Step 1: Python check
Write-Host "[1/6] Checking Python..."
$pyVersion = & python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python not found. Install Python 3.10+ from https://www.python.org/downloads/"
    exit 1
}
Write-Host "    $pyVersion"

# --- Step 2: venv
Write-Host "[2/6] Creating venv..."
if (-not (Test-Path $VenvDir)) {
    & python -m venv $VenvDir
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

# --- Step 3: install deps
Write-Host "[3/6] Installing dependencies..."
& $VenvPip install --upgrade pip --quiet
& $VenvPip install --upgrade -r (Join-Path $ProjectDir "requirements.txt") --quiet

Write-Host "    Installing Playwright Chromium..."
& $VenvPython -m playwright install chromium

# --- Step 4: secrets (.env)
Write-Host "[4/6] Configuring secrets..."

function Get-OrPrompt($key, $envContent, $prompt) {
    $line = $envContent | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) {
        Write-Host "    $key already set in .env"
        return $null
    }
    $val = Read-Host $prompt
    return "$key=$val"
}

$envLines = @()
if (Test-Path $EnvFile) {
    $envLines = @(Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -match "=" })
}
$newLines = @()
$newLines += Get-OrPrompt "ANTHROPIC_API_KEY" $envLines "    Anthropic API key (sk-ant-...)"
$newLines += Get-OrPrompt "TELEGRAM_BOT_TOKEN" $envLines "    Telegram bot token"
$newLines += Get-OrPrompt "TELEGRAM_CHAT_ID" $envLines "    Telegram chat ID (negative number for supergroup)"

$finalLines = @($envLines) + @($newLines | Where-Object { $_ })
# Deduplicate: keep last occurrence of each KEY=...
$seen = @{}
$deduped = New-Object System.Collections.ArrayList
for ($i = $finalLines.Count - 1; $i -ge 0; $i--) {
    $line = $finalLines[$i]
    if ($line -match "^([^=]+)=") {
        $k = $matches[1]
        if (-not $seen.ContainsKey($k)) {
            $seen[$k] = $true
            [void]$deduped.Insert(0, $line)
        }
    }
}
# Write as ASCII with proper newlines (avoids BOM and missing line breaks)
[System.IO.File]::WriteAllLines($EnvFile, $deduped, [System.Text.Encoding]::ASCII)

Write-Host "    Wrote $EnvFile"

# --- Step 5: cookies
Write-Host "[5/6] Checking X cookies file..."
if (-not (Test-Path $CookiesFile)) {
    Write-Host "    x_cookies.json NOT FOUND."
    Write-Host "    Steps to export:"
    Write-Host "      1. Install Cookie-Editor extension: https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm"
    Write-Host "      2. Open x.com (logged in)"
    Write-Host "      3. Cookie-Editor icon -> Export -> Export as JSON"
    Write-Host "      4. Save the file to: $CookiesFile"
    Write-Host ""
    Read-Host "    Press Enter when you've saved x_cookies.json (or Ctrl+C to abort)"
}
if (-not (Test-Path $CookiesFile)) {
    Write-Error "x_cookies.json still missing. Aborting."
    exit 1
}
Write-Host "    Found $CookiesFile"

# --- Step 6: scheduled tasks (2x daily, SGT 8am + 8pm)
Write-Host "[6/6] Registering Windows Scheduled Tasks..."

# Task Scheduler uses LOCAL TIME.
$LocalHours = @(8, 20)
$TaskNames = @("CryptoSignalDigest-0800", "CryptoSignalDigest-2000")

# Also clean up any tasks from previous 4x-daily install.
$LegacyTaskNames = @("CryptoSignalDigest-0900", "CryptoSignalDigest-1500", "CryptoSignalDigest-2100", "CryptoSignalDigest-0300")

# Delete any existing tasks with these names (idempotent reinstall).
# Suppress both stderr and PowerShell's error stream so missing tasks don't abort.
$ErrorActionPreference = "Continue"
foreach ($name in ($TaskNames + $LegacyTaskNames)) {
    cmd /c "schtasks /Delete /TN $name /F >nul 2>&1"
}
$ErrorActionPreference = "Stop"

for ($i = 0; $i -lt $LocalHours.Length; $i++) {
    $hour = $LocalHours[$i]
    $name = $TaskNames[$i]
    $timeStr = "{0:D2}:00" -f $hour
    Write-Host "    Creating task $name at $timeStr local..."
    & schtasks /Create /TN $name /TR "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`"" /SC DAILY /ST $timeStr /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create task $name (exit $LASTEXITCODE)"
        exit 1
    }
}

Write-Host ""
Write-Host "==> Install complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  - Manual run:  .\run.ps1     (or double-click run-manual.bat)"
Write-Host "  - View logs:   Get-Content .\logs\*.log"
Write-Host "  - List tasks:  schtasks /Query /TN CryptoSignalDigest-0800"
Write-Host "  - Remove all:  .\uninstall.ps1"

# --- Step 7: create one-click manual run shortcut
$BatPath = Join-Path $ProjectDir "run-manual.bat"
$BatContent = "@echo off`r`ncd /d `"%~dp0`"`r`npowershell.exe -ExecutionPolicy Bypass -NoExit -File `".\run.ps1`"`r`n"
[System.IO.File]::WriteAllText($BatPath, $BatContent)
Write-Host ""
Write-Host "Created run-manual.bat - double-click it anytime for an instant digest."
