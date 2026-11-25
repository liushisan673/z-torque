$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    Write-Host "[1/5] Build Console assets..."
    Push-Location (Join-Path $RepoRoot "console")
    try {
        npm ci --no-audit --no-fund
        npm run build
    }
    finally {
        Pop-Location
    }

    Write-Host "[2/5] Install desktop build dependencies..."
    python -m pip install --upgrade pip
    python -m pip install -e .
    python -m pip install pyinstaller==6.11.1

    Write-Host "[3/5] Prepare offline Chinese Embedding model..."
    python scripts/download_embedding_model.py
    if ($LASTEXITCODE -ne 0) {
        throw "Preparing the local Embedding model failed with exit code $LASTEXITCODE"
    }

    Write-Host "[4/5] Package Z-Torque..."
    python -m PyInstaller --noconfirm --clean desktop/ZTorque.spec

    Write-Host "[5/5] Verify packaged resources..."
    $ExePath = Join-Path $RepoRoot "dist/ZTorque/ZTorque.exe"
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
    Write-Host "The Chinese Embedding model is included in the package; runtime inference is offline."
}
finally {
    Pop-Location
}
