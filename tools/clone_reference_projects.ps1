# Clones the reference projects CopyClip's design was derived from.
#
# Shallow (--depth 1) single-branch clones: these are read-only references, not forks you
# will commit to. Rerun any time to refresh; existing clones are skipped.
#
# See docs/PRIOR_ART.md for what was actually taken from each one.

$ErrorActionPreference = "Continue"
$dest = Join-Path (Split-Path -Parent $PSScriptRoot) "reference-projects"
New-Item -ItemType Directory -Path $dest -Force | Out-Null
Set-Location $dest

$repos = @(
    @{ name = "scrcpy";            url = "https://github.com/Genymobile/scrcpy.git" },
    @{ name = "ClipCascade";       url = "https://github.com/Sathvik-Rao/ClipCascade.git" },
    @{ name = "Shizuku";           url = "https://github.com/RikkaApps/Shizuku.git" },
    @{ name = "Shizuku-API";       url = "https://github.com/RikkaApps/Shizuku-API.git" },
    @{ name = "awesome-shizuku";   url = "https://github.com/timschneeb/awesome-shizuku.git" },
    @{ name = "clipper";           url = "https://github.com/majido/clipper.git" },
    @{ name = "AdbClipboard";      url = "https://github.com/PRosenb/AdbClipboard.git" },
    @{ name = "ClipboardHelper";   url = "https://github.com/Potato-DiGua/ClipboardHelper.git" },
    @{ name = "kdeconnect-android"; url = "https://github.com/KDE/kdeconnect-android.git" }
)

foreach ($r in $repos) {
    if (Test-Path $r.name) {
        Write-Host "skip   $($r.name) (already cloned)" -ForegroundColor DarkGray
        continue
    }
    Write-Host "clone  $($r.name)..." -ForegroundColor Cyan
    git clone --depth 1 --single-branch $r.url $r.name 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "   ok  $($r.name)" -ForegroundColor Green
    } else {
        Write-Host "   FAILED $($r.name)" -ForegroundColor Red
    }
}

Write-Host "`nDone. See docs/PRIOR_ART.md for what each repo contributed." -ForegroundColor Green
