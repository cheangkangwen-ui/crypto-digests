# uninstall.ps1 — removes scheduled tasks. Does NOT delete project files.

$TaskNames = @("CryptoSignalDigest-0900", "CryptoSignalDigest-1500", "CryptoSignalDigest-2100", "CryptoSignalDigest-0300")
foreach ($name in $TaskNames) {
    schtasks /Delete /TN $name /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removed task: $name"
    } else {
        Write-Host "Task not found: $name"
    }
}
Write-Host "Done. To fully remove: delete this folder."
