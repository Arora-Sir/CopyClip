<#
.SYNOPSIS
    Creates a Start Menu shortcut for the CopyClip tray app.

.DESCRIPTION
    Uses WScript.Shell COM automation, the standard native Windows mechanism for creating
    .lnk files -- no third-party module, matching this project's zero-dependency rule.
    Idempotent: re-running just overwrites the same shortcut in place.

    This does not affect autostart (that is the HKCU Run key tray.py registers itself, plus
    the BackgroundAutomations.ahk / Watchdog.ahk wiring documented in ARCHITECTURE.md) -- this
    is purely for launching or pinning the app manually from the Start Menu.
#>

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $env:SystemRoot "..\Python314\pythonw.exe" | Resolve-Path -ErrorAction SilentlyContinue
if (-not $pythonw) {
    $pythonw = "C:\Python314\pythonw.exe"
}
$trayScript = Join-Path $repoRoot "windows_app\tray.py"
$icon = Join-Path $repoRoot "windows_app\copyclip.ico"

$startMenu = [Environment]::GetFolderPath("Programs")
$shortcutPath = Join-Path $startMenu "CopyClip.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
# No --replace: if CopyClip is already running (the common case, it autostarts), this
# should just no-op rather than kill and restart an already-healthy daemon. Use
# `tray.py --replace` directly from a terminal if you actually want to force a restart.
$shortcut.Arguments = "`"$trayScript`""
$shortcut.WorkingDirectory = $repoRoot
$shortcut.IconLocation = $icon
$shortcut.Description = "CopyClip clipboard sync"
$shortcut.Save()

Write-Output "Created: $shortcutPath"
