$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$PythonVersion = & python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or -not $PythonVersion.StartsWith("3.12.")) {
    throw "Build requires CPython 3.12.x; current interpreter: $PythonVersion"
}

Write-Host "[1/5] Build console assets"
Push-Location (Join-Path $RepoRoot "console")
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "console build failed" }
}
finally {
    Pop-Location
}

Write-Host "[2/5] Prepare isolated Python runtime for MD-Skill"
$RuntimeDir = Join-Path $RepoRoot "build/runtime"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (-not (Test-Path (Join-Path $RuntimeDir "python.exe"))) {
    $RuntimeZip = Join-Path $RepoRoot "build/python-embed-amd64.zip"
    $RuntimeUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Invoke-WebRequest -Uri $RuntimeUrl -OutFile $RuntimeZip
    Expand-Archive -Path $RuntimeZip -DestinationPath $RuntimeDir -Force
}
if (-not (Test-Path (Join-Path $RuntimeDir "python.exe"))) {
    throw "Python embeddable runtime was not found after extraction"
}

Write-Host "[3/5] Install desktop build dependencies"
python -m pip install -e ".[desktop]"
if ($LASTEXITCODE -ne 0) { throw "Python desktop dependencies installation failed" }

Write-Host "[4/5] Package Z-Torque"
$OutputDir = Join-Path $RepoRoot "dist/Z-Torque"
if (Test-Path $OutputDir) { Remove-Item -Recurse -Force $OutputDir }
python -m PyInstaller --clean --noconfirm --distpath (Join-Path $RepoRoot "dist") --workpath (Join-Path $RepoRoot "build/pyinstaller") (Join-Path $RepoRoot "z_torque.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host "[5/5] Verify expected desktop artifacts"
$Executable = Join-Path $OutputDir "Z-Torque.exe"
$BundledPython = Join-Path $OutputDir "_internal/runtime/python.exe"
if (-not (Test-Path $Executable)) { throw "Desktop executable missing: $Executable" }
if (-not (Test-Path $BundledPython)) { throw "Bundled MD-Skill interpreter missing: $BundledPython" }

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $Executable"
Write-Host "Distribute the entire dist/Z-Torque folder, not only the exe."
Write-Host "User data is stored under %LOCALAPPDATA%\Z-Torque."
