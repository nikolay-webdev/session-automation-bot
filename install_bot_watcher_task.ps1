param(
    [string]$TaskName = "GFN Bot Watcher",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$watcherPath = Join-Path $projectDir "watcher.py"

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python wurde nicht gefunden. Übergib den Pfad mit -PythonExe."
    }
    $PythonExe = $pythonCommand.Source
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python-Programm nicht gefunden: $PythonExe"
}

if (-not (Test-Path -LiteralPath $watcherPath)) {
    throw "watcher.py nicht gefunden: $watcherPath"
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$watcherPath`"" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Starts the Session Automation Bot watcher at Windows logon." `
    -Force | Out-Null

Write-Host "Geplante Aufgabe installiert: $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
