$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found: $Python. Create venv and install build dependencies first."
}

$BasePythonDir = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
$TclSource = Join-Path $BasePythonDir "tcl"
$BundledTcl = Join-Path $ProjectDir "build_resources\tcl"

if (-not (Test-Path -LiteralPath (Join-Path $TclSource "tcl8.6\init.tcl"))) {
    throw "The Python installation does not contain a complete Tcl/Tk runtime: $TclSource"
}

# Keeping a build-local copy avoids Tcl path access issues during PyInstaller analysis.
New-Item -ItemType Directory -Force -Path $BundledTcl | Out-Null
Copy-Item -Recurse -Force -LiteralPath (Join-Path $TclSource "tcl8.6") -Destination $BundledTcl
Copy-Item -Recurse -Force -LiteralPath (Join-Path $TclSource "tk8.6") -Destination $BundledTcl
$env:TCL_LIBRARY = Join-Path $BundledTcl "tcl8.6"
$env:TK_LIBRARY = Join-Path $BundledTcl "tk8.6"

& $Python (Join-Path $ProjectDir "tools\generate_icon.py")
& $Python -m PyInstaller --noconfirm --clean (Join-Path $ProjectDir "WorkMonitorV2.spec")

Write-Host "Build complete: $ProjectDir\dist\WorkMonitorV2.exe"
