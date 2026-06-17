$ErrorActionPreference = "Stop"

$PidFile = Join-Path $PSScriptRoot "data\qdii-monitor.pid"
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "QDII monitor is not running (PID file not found)."
    exit 0
}

$StoredPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
if ($StoredPid -notmatch "^\d+$") {
    throw "Invalid PID file: $PidFile"
}

$ProcessId = [int]$StoredPid
$ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
if ($null -eq $ProcessInfo) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "QDII monitor process is no longer running; stale PID file removed."
    exit 0
}

$CommandLine = [string]$ProcessInfo.CommandLine
if ($CommandLine -notmatch "uvicorn\s+qdii_monitor\.app:app") {
    throw "PID $ProcessId does not belong to the QDII monitor; refusing to stop it."
}

Stop-Process -Id $ProcessId
Remove-Item -LiteralPath $PidFile -Force
Write-Host "QDII monitor stopped (PID $ProcessId)."
