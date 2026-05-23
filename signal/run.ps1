# run.ps1 — invoked by Task Scheduler; activates venv and runs the digest.
# Logs go to logs\YYYY-MM-DD_HH-MM.log

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Script = Join-Path $ProjectDir "read_crypto_signals.py"
$LogDir = Join-Path $ProjectDir "logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$ts = Get-Date -Format "yyyy-MM-dd_HH-mm"
$LogFile = Join-Path $LogDir "$ts.log"

# Ensure UTF-8 stdout (Phase 1 lesson)
$env:PYTHONIOENCODING = "utf-8"
# Unbuffered output so we see live progress
$env:PYTHONUNBUFFERED = "1"

Write-Output "=== Run started $(Get-Date -Format 'o') ===" | Tee-Object -FilePath $LogFile -Append
& $VenvPython -u $Script 2>&1 | Tee-Object -FilePath $LogFile -Append
$exitCode = $LASTEXITCODE
Write-Output "=== Run finished $(Get-Date -Format 'o') exit=$exitCode ===" | Tee-Object -FilePath $LogFile -Append
exit $exitCode
