$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'workbench\.state\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No workbench process record.'; exit 0 }
$workbenchPid = [int](Get-Content -LiteralPath $pidFile)
$parent = Get-CimInstance Win32_Process -Filter "ProcessId = $workbenchPid"
if (-not $parent) { Write-Output 'Workbench is already stopped.'; exit 0 }
$entryPath = Join-Path $PSScriptRoot 'scripts\workbench.py'
if ($parent.CommandLine -notlike ('*' + $entryPath + '*')) {
    throw 'The process record belongs to another program; nothing was stopped.'
}
# Windows virtual environments may launch the configured Python runtime as a child.
$children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $workbenchPid"
foreach ($child in $children) {
    if ($child.CommandLine -like ('*' + $entryPath + '*')) {
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
}
Stop-Process -Id $workbenchPid -ErrorAction SilentlyContinue
Write-Output 'Workbench stopped. Production files are unchanged.'
