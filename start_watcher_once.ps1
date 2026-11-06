param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = Join-Path $projectDir "wake_log.txt"
$envFile = Join-Path $projectDir ".env"
$watcherPath = Join-Path $projectDir "watcher.py"
$mainPath = Join-Path $projectDir "main.py"

function Redact-SensitiveText {
    param([string]$Message)

    $safeMessage = $Message
    $safeMessage = [regex]::Replace(
        $safeMessage,
        '(?i)(https?://api\.telegram\.org/bot)[^/\s]+',
        '$1[REDACTED]'
    )
    $safeMessage = [regex]::Replace(
        $safeMessage,
        '(?i)(BOT_TOKEN|DEFAULT_LOGIN|DEFAULT_PASSWORD|PASSWORD|SECRET|API_KEY)\s*[=:]\s*[^\s,;]+',
        '$1=[REDACTED]'
    )
    $safeMessage = [regex]::Replace(
        $safeMessage,
        '(?i)(Cookie|Set-Cookie|Authorization)\s*:\s*[^\r\n]+',
        '$1: [REDACTED]'
    )
    $safeMessage = [regex]::Replace(
        $safeMessage,
        '(?i)([A-Z]:\\Users\\)[^\\/:*?"<>|]+',
        '$1[REDACTED]'
    )

    return $safeMessage
}

function Write-WakeLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $safeMessage = Redact-SensitiveText $Message
    Add-Content -LiteralPath $logFile -Value "[$timestamp] $safeMessage"
}

$createdNew = $false
$wakeHelperMutex = [System.Threading.Mutex]::new(
    $true,
    "Local\GFNWakeHelperPersistent",
    [ref]$createdNew
)

if (-not $createdNew) {
    Write-WakeLog "Ein anderer Wake-Helper läuft bereits. Diese Instanz wird beendet."
    exit 0
}

function Load-EnvFile {
    if (-not (Test-Path -LiteralPath $envFile)) {
        return
    }

    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }

        $parts = $line.Split("=", 2)
        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")

        if ($name) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Send-Telegram {
    param([string]$Text)

    $token = [Environment]::GetEnvironmentVariable("BOT_TOKEN", "Process")
    $chatId = [Environment]::GetEnvironmentVariable("ALLOWED_CHAT_ID", "Process")

    if (-not $token -or -not $chatId) {
        Write-WakeLog "Telegram-Nachricht übersprungen: BOT_TOKEN oder ALLOWED_CHAT_ID fehlt."
        return
    }

    try {
        $uri = "https://api.telegram.org/bot$token/sendMessage"
        Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body (@{
            chat_id = $chatId
            text = $Text
        } | ConvertTo-Json -Compress) | Out-Null
        Write-WakeLog "Telegram-Nachricht gesendet: $Text"
    }
    catch {
        Write-WakeLog "Senden der Telegram-Nachricht fehlgeschlagen: $($_.Exception.Message)"
    }
}

function Test-WatcherRunning {
    $pythonProcesses = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match "python" -and
            (
                $_.CommandLine -match [regex]::Escape($watcherPath) -or
                $_.CommandLine -match "(^|[\\/\s])watcher\.py(\s|$)"
            )
        }

    return [bool]$pythonProcesses
}

function Test-BotRunning {
    $pythonProcesses = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match "python" -and
            (
                $_.CommandLine -match [regex]::Escape($mainPath) -or
                $_.CommandLine -match "(^|[\\/\s])main\.py(\s|$)"
            )
        }

    return [bool]$pythonProcesses
}

function Initialize-KeepAwakeApi {
    if ("WakeHelper.Power" -as [type]) {
        return
    }

    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace WakeHelper {
    public static class Power {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint SetThreadExecutionState(uint esFlags);
    }
}
"@
}

function Invoke-KeepAwakeSignal {
    Initialize-KeepAwakeApi

    $ES_CONTINUOUS = [uint32]2147483648
    $ES_SYSTEM_REQUIRED = [uint32]1
    $ES_DISPLAY_REQUIRED = [uint32]2
    $flags = $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_DISPLAY_REQUIRED

    [WakeHelper.Power]::SetThreadExecutionState($flags) | Out-Null
}

function Start-PersistentKeepAwake {
    Write-WakeLog "Persistenter Keep-Awake-Prozess im Wake-Helper wird gestartet."

    try {
        Invoke-KeepAwakeSignal
        Write-WakeLog "Persistenter Keep-Awake-Prozess im Wake-Helper gestartet. PID=$PID"

        while ($true) {
            Start-Sleep -Seconds 60
            Invoke-KeepAwakeSignal
            Write-WakeLog "Persistenter Keep-Awake-Prozess im Wake-Helper weiterhin aktiv. PID=$PID"
        }
    }
    catch {
        Write-WakeLog "Persistenter Keep-Awake-Prozess im Wake-Helper fehlgeschlagen: $($_.Exception.Message)"
        throw
    }
    finally {
        if ("WakeHelper.Power" -as [type]) {
            $ES_CONTINUOUS = [uint32]2147483648
            [WakeHelper.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
        }
        Write-WakeLog "Persistenter Keep-Awake-Prozess im Wake-Helper beendet. PID=$PID"
    }
}

Set-Location -LiteralPath $projectDir
Load-EnvFile

Write-WakeLog "Wake-Helper gestartet. Warte auf das Netzwerk..."
try {
    Invoke-KeepAwakeSignal
    Write-WakeLog "Initiales Keep-Awake-Signal gesendet."
}
catch {
    Write-WakeLog "Initiales Keep-Awake-Signal fehlgeschlagen: $($_.Exception.Message)"
}

foreach ($secondsWaited in 15, 30, 45, 60, 75, 90) {
    Start-Sleep -Seconds 15
    try {
        Invoke-KeepAwakeSignal
    }
    catch {
        Write-WakeLog "Keep-Awake-Signal während der Startwartezeit fehlgeschlagen: $($_.Exception.Message)"
    }
    Write-WakeLog "Wake-Helper wartet weiterhin auf Netzwerk/Start... ${secondsWaited}s"
}

if (Test-WatcherRunning) {
    Write-WakeLog "watcher.py läuft bereits. Kein zweiter Start."
    Send-Telegram "Laptop ist aufgewacht. Watcher laeuft bereits. Kein zweiter Start."
    Start-PersistentKeepAwake
    exit 0
}

if (Test-BotRunning) {
    Write-WakeLog "main.py läuft bereits. Kein Watcher-Start erforderlich."
    Send-Telegram "Laptop ist aufgewacht. Bot laeuft bereits. Kein zweiter Start."
    Start-PersistentKeepAwake
    exit 0
}

Write-WakeLog "watcher.py läuft nicht. Watcher wird gestartet."
Start-Process -FilePath $PythonExe -ArgumentList @($watcherPath) -WorkingDirectory $projectDir -WindowStyle Hidden
Send-Telegram "Laptop ist aufgewacht. Watcher wurde gestartet."
Start-PersistentKeepAwake
