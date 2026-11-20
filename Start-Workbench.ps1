$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'runtime\video-harness\.venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'scripts\workbench.py'
$statePath = Join-Path $projectRoot 'workbench\.state'
$workbenchUrl = 'http://127.0.0.1:8787/'
try {
    $session = Invoke-RestMethod -Uri ($workbenchUrl + 'api/session') -TimeoutSec 2
    if ($session.app -ne 'story-video-workbench') { throw 'Port 8787 belongs to another application.' }
    Write-Output $workbenchUrl
    exit 0
} catch {
    if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 8787 is already occupied. Start scripts/workbench.py with another --port.'
    }
}
New-Item -ItemType Directory -Path $statePath -Force | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', ('"' + $entryPath + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $statePath 'server.log') -RedirectStandardError (Join-Path $statePath 'server-error.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $statePath 'server.pid')
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $session = Invoke-RestMethod -Uri ($workbenchUrl + 'api/session') -TimeoutSec 1
        if ($session.app -eq 'story-video-workbench') { Write-Output $workbenchUrl; exit 0 }
    } catch {}
    if ($process.HasExited) { throw 'Workbench exited. See workbench/.state/server-error.log.' }
}
throw 'Workbench did not become ready. See workbench/.state/server-error.log.'
