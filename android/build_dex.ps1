# Builds ClipBridge.java into bin\clipbridge.dex
#
# Compiles against the android.jar matching the phone (SDK 36) and dexes with d8.
# You can rerun this any time to rebuild the helper from source, so nothing shipped
# in bin\ has to be taken on trust.

$ErrorActionPreference = "Stop"

$root      = Split-Path -Parent $PSScriptRoot
$srcDir    = Join-Path $root "android"
$outDir    = Join-Path $root "bin"
$classDir  = Join-Path $env:TEMP "copyclip_classes"

# --- locate the SDK ------------------------------------------------------
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $sdk)) { throw "Android SDK not found. Set ANDROID_HOME." }

# Prefer the platform matching the phone; fall back to the newest available.
$targetApi = 36
$androidJar = Join-Path $sdk "platforms\android-$targetApi\android.jar"
if (-not (Test-Path $androidJar)) {
    $newest = Get-ChildItem (Join-Path $sdk "platforms") -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName "android.jar") } |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $newest) { throw "No android.jar found under $sdk\platforms" }
    $androidJar = Join-Path $newest.FullName "android.jar"
}

# Newest build-tools that actually ships d8.
# Use lib\d8.jar rather than d8.bat: the batch wrapper splits Windows paths at the
# drive-letter colon and hands d8 a bare "C", which fails with InvalidPathException.
$d8Jar = Get-ChildItem (Join-Path $sdk "build-tools") -Directory |
    Sort-Object { [version]($_.Name -replace '[^0-9.].*$','') } -Descending |
    ForEach-Object { Join-Path $_.FullName "lib\d8.jar" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $d8Jar) { throw "lib\d8.jar not found in $sdk\build-tools" }

# javac: PATH first, then Android Studio's bundled JBR.
$javac = (Get-Command javac -ErrorAction SilentlyContinue).Source
if (-not $javac) { $javac = "C:\Program Files\Android\Android Studio\jbr\bin\javac.exe" }
if (-not (Test-Path $javac)) { throw "javac not found. Install a JDK or Android Studio." }
$java = Join-Path (Split-Path -Parent $javac) "java.exe"
if (-not (Test-Path $java)) { throw "java.exe not found next to javac" }

Write-Host "android.jar : $androidJar"
Write-Host "d8          : $d8Jar"
Write-Host "javac       : $javac"

# --- compile -------------------------------------------------------------
if (Test-Path $classDir) { Remove-Item $classDir -Recurse -Force }
New-Item -ItemType Directory -Path $classDir -Force | Out-Null
New-Item -ItemType Directory -Path $outDir   -Force | Out-Null

# android/android/content/IContentProvider.java is a 5-line stub exposing the one hidden
# type ClipBridge's FakeContext needs (android.content.IContentProvider is @hide) -- the
# real interface is supplied by the boot classloader at runtime. Compiles fine against the
# stock android.jar above; no patched hidden-API jar needed. See
# otherCopyProjects/NOTES.md's corrected FakeContext section and
# bugs/SPIKE-IMAGE-CLIPBOARD.md for what this was verified against.
#
# -source/-target rather than --release: --release pins the JDK platform API and
# conflicts with compiling against android.jar. -Xlint:-options silences the
# expected "bootstrap class path not set" note.
& $javac -source 17 -target 17 -Xlint:-options -nowarn `
    -cp $androidJar -d $classDir `
    (Join-Path $srcDir "ClipBridge.java") `
    (Join-Path $srcDir "android\content\IContentProvider.java")
if ($LASTEXITCODE -ne 0) { throw "javac failed ($LASTEXITCODE)" }

# --- dex -----------------------------------------------------------------
# @() is load-bearing: with a single .class file this yields a String rather than an
# array, and splatting a scalar String splits it at the drive-letter colon, handing d8
# a bare "C". Wrapping in @() keeps it an array of one.
$classes = @(Get-ChildItem $classDir -Recurse -Filter *.class | ForEach-Object { $_.FullName })
if ($classes.Count -eq 0) { throw "no .class files produced" }

& $java -cp $d8Jar com.android.tools.r8.D8 `
    --release --min-api $targetApi --lib $androidJar --output $outDir @classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed ($LASTEXITCODE)" }

$produced = Join-Path $outDir "classes.dex"
$final    = Join-Path $outDir "clipbridge.dex"
if (-not (Test-Path $produced)) { throw "d8 produced no classes.dex" }
Move-Item $produced $final -Force
Remove-Item $classDir -Recurse -Force

$size = [math]::Round((Get-Item $final).Length / 1KB, 1)
Write-Host "OK -> $final ($size KB)" -ForegroundColor Green
