# Removes the CopyClip logon task and stops it if running.

$ErrorActionPreference = "Stop"
$taskName = "CopyClip"

if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Host "No scheduled task named '$taskName'. Nothing to do."
    return
}

try { Stop-ScheduledTask -TaskName $taskName } catch { }
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Removed scheduled task '$taskName'." -ForegroundColor Green

Get-Process pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*copyclip.py*" } |
    ForEach-Object { Write-Host "Stopping PID $($_.Id)"; Stop-Process -Id $_.Id -Force }
