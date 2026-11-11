$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
try {
    Set-Location -LiteralPath $projectRoot
    Get-Command node -ErrorAction Stop | Out-Null
    # Python owns the same startup lock used by npm start/dev:local.
    # It verifies cwd, command line and creation time before stopping services.
    & node tools/python.cjs tools/start-local.py --restart
    if ($LASTEXITCODE -ne 0) {
        throw 'Restart failed. Check the error above and .runtime/local-api.log / local-worker.log.'
    }
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
