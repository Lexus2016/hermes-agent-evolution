<#
.SYNOPSIS
    Schedule the OFFICIAL `hermes update --yes` to run daily on Windows.

.DESCRIPTION
    Windows has no cron, so the bash scripts/install_auto_update.sh cannot run
    here. This is the Windows equivalent: it registers a Task Scheduler job that
    runs `hermes update --yes` once a day. `hermes update` updates the real
    install dir, has built-in snapshot/rollback, and pulls THIS fork's `origin`
    (evolution) plus `upstream` (NousResearch) — same behaviour as the cron path.

    Idempotent: re-running overwrites the existing task (/F). Off-zero minute
    (04:17) on purpose to avoid the :00 thundering herd.

.PARAMETER Remove
    Delete the scheduled task instead of creating it.

.PARAMETER Time
    Daily run time as HH:mm (24h). Default 04:17.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/install_auto_update.ps1
    powershell -ExecutionPolicy Bypass -File scripts/install_auto_update.ps1 -Time 05:30
    powershell -ExecutionPolicy Bypass -File scripts/install_auto_update.ps1 -Remove
#>
param(
    [switch]$Remove,
    [string]$Time = "04:17"
)

$ErrorActionPreference = "Stop"
$TaskName = "HermesEvolutionUpdate"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task '$TaskName' to remove."
    }
    exit 0
}

# Resolve the hermes executable. Prefer an absolute path so the task does not
# depend on the service account's PATH; fall back to bare 'hermes' if unknown.
$hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
if ($hermesCmd) { $hermes = $hermesCmd.Source } else { $hermes = "hermes" }

# Ensure the log directory exists.
$logDir = Join-Path $env:USERPROFILE ".hermes\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "auto-update.log"

# Build the action. Wrap in cmd /c so we can redirect stdout/stderr to the log.
# Inner quoting: the whole /TR value is one string; quote the exe and log paths.
$action = "cmd /c `"`"$hermes`" update --yes >> `"$log`" 2>&1`""

schtasks /Create /SC DAILY /ST $Time /TN $TaskName /TR $action /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to register scheduled task '$TaskName' (schtasks exit $LASTEXITCODE)."
    exit 1
}

Write-Host "[OK] Scheduled daily 'hermes update --yes' at $Time (task: $TaskName)."
Write-Host "     Log:    $log"
Write-Host "     Remove: powershell -ExecutionPolicy Bypass -File scripts/install_auto_update.ps1 -Remove"
Write-Host ""
Write-Host "NOTE: self-update needs the install's git 'origin' to point at this fork."
Write-Host "      See AUTO_UPGRADE.md for switching an existing install onto the fork."
