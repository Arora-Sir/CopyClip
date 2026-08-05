# Run CopyClip in the foreground with live logging. Ctrl+C stops it.
# For a windowless background run use start_silent.vbs instead.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not found on PATH" }

if (-not (Test-Path (Join-Path $PSScriptRoot "bin\clipbridge.dex"))) {
    Write-Host "clipbridge.dex missing, building it first..." -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot "android\build_dex.ps1")
}

& $py -u (Join-Path $PSScriptRoot "copyclip.py") @args
