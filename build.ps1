$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ installation with Tk is required.' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
    & '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --distpath . MARS_KIS.spec
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
    Write-Host 'Build complete. Distribute the single TOP100 EXE in this folder.'
} finally {
    Pop-Location
}
