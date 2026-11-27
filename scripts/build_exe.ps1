$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    Write-Host "[1/6] Build Console assets..."
    Push-Location (Join-Path $RepoRoot "console")
    try {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) {
            throw "Installing console dependencies failed with exit code $LASTEXITCODE"
        }
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Building the console failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "[2/6] Install desktop build dependencies..."
    python -m pip install --upgrade pip
    python -m pip install -e .
    python -m pip install pyinstaller==6.11.1

    Write-Host "[3/6] Prepare offline Chinese Embedding model..."
    python scripts/download_embedding_model.py
    if ($LASTEXITCODE -ne 0) {
        throw "Preparing the local Embedding model failed with exit code $LASTEXITCODE"
    }

    Write-Host "[4/6] Prepare sanitized seed data (agents / skills / knowledge base)..."
    python scripts/prepare_seed_data.py
    if ($LASTEXITCODE -ne 0) {
        throw "Preparing seed data failed with exit code $LASTEXITCODE"
    }

    Write-Host "[5/6] Package Z-Torque..."
    # Use a brand-new work/dist path on every build: PyInstaller's COLLECT step removes
    # stale output, which can be blocked by a running instance, antivirus or a workspace
    # bulk-delete guard. Never reusing a path avoids that deletion entirely.
    $BuildStamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $WorkDir = Join-Path $RepoRoot "build-$BuildStamp"
    $StageDir = Join-Path $RepoRoot "dist-stage-$BuildStamp"
    python -m PyInstaller --noconfirm --workpath $WorkDir --distpath $StageDir desktop/ZTorque.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    $PackageDir = Join-Path $RepoRoot "dist/ZTorque"
    $Running = Get-Process -Name ZTorque -ErrorAction SilentlyContinue
    if ($Running) {
        throw "ZTorque is running (PID: $($Running.Id -join ', ')). Close the app before packaging so its DLLs are not locked."
    }
    New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null
    Copy-Item -Path (Join-Path $StageDir "ZTorque/*") -Destination $PackageDir -Recurse -Force

    Write-Host "[6/6] Verify packaged resources..."
    $ExePath = Join-Path $PackageDir "ZTorque.exe"
    if (-not (Test-Path $ExePath)) {
        throw "Packaged executable not found: $ExePath"
    }
    & $ExePath --self-check
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged executable self-check failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Build complete:"
    Write-Host "  $ExePath"
    Write-Host "Distribute the complete dist/ZTorque directory, not the EXE alone."
    Write-Host "The Chinese Embedding model and sanitized business data are included; runtime inference is offline."
    Write-Host "Built-in account: admin / admin123. First launch will prompt for a model API key."
}
finally {
    Pop-Location
}
