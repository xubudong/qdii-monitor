$ErrorActionPreference = "Stop"

$HostAddress = if ($env:WEB_HOST) { $env:WEB_HOST } else { "127.0.0.1" }
$Port = if ($env:WEB_PORT) { [int]$env:WEB_PORT } else { 8010 }
$PidFile = Join-Path $PSScriptRoot "data\qdii-monitor.pid"
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
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "qdii_monitor.app:app", "--host", $HostAddress, "--port", $Port) `
    -WorkingDirectory $PSScriptRoot `
    -NoNewWindow `
    -PassThru
Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII

try {
    $Process.WaitForExit()
    exit $Process.ExitCode
}
finally {
    if (Test-Path -LiteralPath $PidFile) {
        $StoredPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
        if ($StoredPid -eq [string]$Process.Id) {
            Remove-Item -LiteralPath $PidFile -Force
        }
    }
}
