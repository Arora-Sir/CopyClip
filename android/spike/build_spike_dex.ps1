# THROWAWAY build script for the image-clipboard spike (android/spike/ImageSpike.java).
# Mirrors android/build_dex.ps1 exactly, but compiles two sources (the spike class plus its
# IContentProvider stub) into bin/spike.dex. Not referenced by production code.

$ErrorActionPreference = "Stop"

$root      = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$srcDir    = $PSScriptRoot
$outDir    = Join-Path $root "bin"
$classDir  = Join-Path $env:TEMP "copyclip_spike_classes"

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $sdk)) { throw "Android SDK not found. Set ANDROID_HOME." }

$targetApi = 36
$androidJar = Join-Path $sdk "platforms\android-$targetApi\android.jar"
if (-not (Test-Path $androidJar)) {
    $newest = Get-ChildItem (Join-Path $sdk "platforms") -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName "android.jar") } |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $newest) { throw "No android.jar found under $sdk\platforms" }
    $androidJar = Join-Path $newest.FullName "android.jar"
}

$d8Jar = Get-ChildItem (Join-Path $sdk "build-tools") -Directory |
    Sort-Object { [version]($_.Name -replace '[^0-9.].*$','') } -Descending |
    ForEach-Object { Join-Path $_.FullName "lib\d8.jar" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $d8Jar) { throw "lib\d8.jar not found in $sdk\build-tools" }

$javac = (Get-Command javac -ErrorAction SilentlyContinue).Source
if (-not $javac) { $javac = "C:\Program Files\Android\Android Studio\jbr\bin\javac.exe" }
if (-not (Test-Path $javac)) { throw "javac not found. Install a JDK or Android Studio." }
$java = Join-Path (Split-Path -Parent $javac) "java.exe"
if (-not (Test-Path $java)) { throw "java.exe not found next to javac" }

Write-Host "android.jar : $androidJar"
Write-Host "d8          : $d8Jar"
Write-Host "javac       : $javac"

if (Test-Path $classDir) { Remove-Item $classDir -Recurse -Force }
New-Item -ItemType Directory -Path $classDir -Force | Out-Null
New-Item -ItemType Directory -Path $outDir   -Force | Out-Null

$sources = @(
    (Join-Path $srcDir "ImageSpike.java"),
    (Join-Path $srcDir "android\content\IContentProvider.java")
)

& $javac -source 17 -target 17 -Xlint:-options -nowarn `
    -cp $androidJar -d $classDir @sources
if ($LASTEXITCODE -ne 0) { throw "javac failed ($LASTEXITCODE)" }

$classes = @(Get-ChildItem $classDir -Recurse -Filter *.class | ForEach-Object { $_.FullName })
if ($classes.Count -eq 0) { throw "no .class files produced" }

& $java -cp $d8Jar com.android.tools.r8.D8 `
    --release --min-api $targetApi --lib $androidJar --output $outDir @classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed ($LASTEXITCODE)" }

$produced = Join-Path $outDir "classes.dex"
$final    = Join-Path $outDir "spike.dex"
if (-not (Test-Path $produced)) { throw "d8 produced no classes.dex" }
Move-Item $produced $final -Force
Remove-Item $classDir -Recurse -Force

$size = [math]::Round((Get-Item $final).Length / 1KB, 1)
Write-Host "OK -> $final ($size KB)" -ForegroundColor Green
