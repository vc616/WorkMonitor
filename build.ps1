$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# PyInstaller writes dist/ and build/ relative to the current directory.
Set-Location -LiteralPath $ProjectDir

# Workspaces may use venv/, .venv/ or .venv-dev/. Pick the first interpreter
# that actually has PyInstaller installed.
$Python = $null
foreach ($candidate in @(
    (Join-Path $ProjectDir "venv\Scripts\python.exe"),
    (Join-Path $ProjectDir ".venv\Scripts\python.exe"),
    (Join-Path $ProjectDir ".venv-dev\Scripts\python.exe")
)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    & $candidate -c "import PyInstaller" *> $null
    if ($?) { $Python = $candidate; break }
}
if (-not $Python) {
    throw "No virtual environment with PyInstaller found. Run: <venv>\Scripts\python.exe -m pip install -r requirements-build.txt"
}
Write-Host "Using interpreter: $Python"

$BasePythonDir = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
$TclSource = Join-Path $BasePythonDir "tcl"
$BundledTcl = Join-Path $ProjectDir "build_resources\tcl"

if (-not (Test-Path -LiteralPath (Join-Path $TclSource "tcl8.6\init.tcl")) -or
    -not (Test-Path -LiteralPath (Join-Path $TclSource "tk8.6") -PathType Container)) {
    throw "The Python installation does not contain a complete Tcl/Tk runtime: $TclSource"
}

# Keeping a build-local copy avoids Tcl path access issues during PyInstaller
# analysis. Copy contents into explicit targets so repeated builds overwrite
# files instead of nesting a second tcl8.6/tk8.6 directory.
$TclTarget = Join-Path $BundledTcl "tcl8.6"
$TkTarget = Join-Path $BundledTcl "tk8.6"
New-Item -ItemType Directory -Force -Path $TclTarget | Out-Null
New-Item -ItemType Directory -Force -Path $TkTarget | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $TclSource "tcl8.6\*") -Destination $TclTarget
Copy-Item -Recurse -Force -Path (Join-Path $TclSource "tk8.6\*") -Destination $TkTarget
$env:TCL_LIBRARY = Join-Path $BundledTcl "tcl8.6"
$env:TK_LIBRARY = Join-Path $BundledTcl "tk8.6"

$IconPath = Join-Path $ProjectDir "assets\work_monitor.ico"
if (-not (Test-Path -LiteralPath $IconPath)) {
    & $Python (Join-Path $ProjectDir "tools\generate_icon.py")
}
& $Python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $ProjectDir "dist") `
    --workpath (Join-Path $ProjectDir "build") `
    (Join-Path $ProjectDir "WorkMonitorV2.spec")

Write-Host "Build complete: $ProjectDir\dist\WorkMonitorV2.exe"
