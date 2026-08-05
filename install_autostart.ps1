# Registers CopyClip to start silently at logon via Task Scheduler.
#
# This is a persistent system change, so it is deliberately a separate opt-in step and is
# never run automatically. Undo it with uninstall_autostart.ps1.
#
# Task Scheduler rather than a Startup-folder shortcut because it survives Explorer
# restarts, can restart the task if it dies, and does not flash a window.

$ErrorActionPreference = "Stop"

$taskName = "CopyClip"
$vbs = Join-Path $PSScriptRoot "start_silent.vbs"
if (-not (Test-Path $vbs)) { throw "start_silent.vbs not found next to this script" }

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Task '$taskName' already exists. Removing it first..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" `
                                   -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Runs as the interactive user (required: the clipboard is per session, a SYSTEM task
# cannot reach it). Limit disabled so the daemon is not killed after 3 days.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                          -DontStopIfGoingOnBatteries `
                                          -StartWhenAvailable `
                                          -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                                          -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings `
                       -Description "Bidirectional clipboard sync with s24-ultra over Tailscale" | Out-Null

Write-Host "Registered scheduled task '$taskName' (starts at logon)." -ForegroundColor Green
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $taskName"
