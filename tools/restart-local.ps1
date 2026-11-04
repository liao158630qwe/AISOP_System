$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $projectRoot '.runtime'
$startupLock = $null

function Get-ServiceTree {
    param($ServiceProcess, $ProcessSnapshot)

    foreach ($childProcess in $ProcessSnapshot) {
        if ($childProcess.ParentProcessId -eq $ServiceProcess.ProcessId -and
            $childProcess.CreationDate -ge $ServiceProcess.CreationDate) {
            Get-ServiceTree $childProcess $ProcessSnapshot
        }
    }
    $ServiceProcess
}

try {
    Set-Location -LiteralPath $projectRoot
    Get-Command node -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $startupLock = [System.IO.File]::Open(
        (Join-Path $runtimeDirectory 'local-start.lock'),
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    & node tools/python.cjs -c 'import backend.app'
    if ($LASTEXITCODE -ne 0) {
        throw 'Python dependencies are unavailable. Existing services were left running.'
    }

    $recordedIds = @()
    $processFile = Join-Path $runtimeDirectory 'local-processes.json'
    if (Test-Path -LiteralPath $processFile) {
        try {
            $recordedProcesses = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
            $recordedIds = @($recordedProcesses.api, $recordedProcesses.worker)
        } catch {
            Write-Warning 'Cannot read saved process IDs; checking project Python processes instead.'
        }
    }

    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $processSnapshot = @(Get-CimInstance Win32_Process)
    $serviceRoots = @($processSnapshot | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        $_.CommandLine -match '-m\s+(uvicorn\s+backend\.app:create_app|backend\.worker)(\s|$)' -and
        ($_.ExecutablePath -eq $projectPython -or $_.ProcessId -in $recordedIds)
    })
    $serviceTree = @($serviceRoots | ForEach-Object {
        Get-ServiceTree $_ $processSnapshot
    })
    $serviceIds = @($serviceTree | ForEach-Object { $_.ProcessId })
    $portOwners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq 8000 } |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerId in $portOwners) {
        if ($ownerId -notin $serviceIds) {
            throw "Port 8000 belongs to another process (PID $ownerId). Free the port and try again."
        }
    }

    foreach ($serviceProcess in $serviceTree) {
        $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($serviceProcess.ProcessId)"
        if ($null -ne $currentProcess -and $currentProcess.CreationDate -eq $serviceProcess.CreationDate) {
            Write-Host "Stopping service process $($serviceProcess.ProcessId)..."
            try {
                Stop-Process -Id $serviceProcess.ProcessId -Force -ErrorAction Stop
            } catch {
                # A venv launcher can exit as soon as its child is stopped.
                if (Get-Process -Id $serviceProcess.ProcessId -ErrorAction SilentlyContinue) {
                    throw
                }
            }
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ($serviceIds.Count -gt 0 -and @(Get-Process -Id $serviceIds -ErrorAction SilentlyContinue).Count -gt 0) {
        if ((Get-Date) -ge $deadline) {
            throw 'Timed out waiting for the old services to stop.'
        }
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 8000 }) {
        throw 'Port 8000 is still occupied. Startup was cancelled.'
    }

    Write-Host 'Starting API and Worker...'
    & node tools/python.cjs tools/start-local.py
    if ($LASTEXITCODE -ne 0) {
        throw 'Startup failed. Check .runtime/local-api.log and .runtime/local-worker.log.'
    }
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($null -ne $startupLock) {
        $startupLock.Dispose()
    }
}
