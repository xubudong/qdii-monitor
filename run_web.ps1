$ErrorActionPreference = "Stop"

$HostAddress = if ($env:WEB_HOST) { $env:WEB_HOST } else { "127.0.0.1" }
$Port = if ($env:WEB_PORT) { [int]$env:WEB_PORT } else { 8010 }
$PidFile = Join-Path $PSScriptRoot "data\qdii-monitor.pid"
$LogFile = if ($env:QDII_LOG_FILE) { $env:QDII_LOG_FILE } else { Join-Path $PSScriptRoot "data\qdii-monitor.log" }
$ErrorLogFile = [System.IO.Path]::ChangeExtension($LogFile, ".error.log")
$Python = if (Test-Path ".venv\Scripts\python.exe") {
    (Resolve-Path ".venv\Scripts\python.exe").Path
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    (Get-Command py).Source
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    (Get-Command python).Source
} else {
    throw "Python not found. Create .venv and install requirements.txt first."
}

Write-Host "Starting QDII monitor at http://$HostAddress`:$Port"
New-Item -ItemType Directory -Force -Path (Split-Path $PidFile) | Out-Null
$LogDir = Split-Path $LogFile
if ($LogDir) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
if (Test-Path -LiteralPath $PidFile) {
    $StoredPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($StoredPid -match "^\d+$") {
        $Existing = Get-CimInstance Win32_Process -Filter "ProcessId = $StoredPid" -ErrorAction SilentlyContinue
        if ($null -ne $Existing -and [string]$Existing.CommandLine -match "uvicorn\s+qdii_monitor\.app:app") {
            throw "QDII monitor already running (PID $StoredPid)."
        }
    }
    Remove-Item -LiteralPath $PidFile -Force
}
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "qdii_monitor.app:app", "--host", $HostAddress, "--port", $Port) `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrorLogFile `
    -PassThru
Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII
Write-Host "PID: $($Process.Id)"
Write-Host "Log: $LogFile"
Write-Host "Error log: $ErrorLogFile"
