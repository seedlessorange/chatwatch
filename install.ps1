# chatwatch for Windows: runs at sign-in, page on http://127.0.0.1:7731, alerts every 5 minutes.
# Run in PowerShell from the chatwatch folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
# To remove it:                                  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
# UNTESTED on Windows so far. If something fails, please send the error text back.
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$Task = "chatwatch"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "chatwatch removed. Its files in ~\.claude (chatwatch-*.json*, handoffs\) are left in place."
    exit 0
}

# Find Python 3.8+ (the py launcher first, then python on PATH)
$py = $null
foreach ($c in @("py -3", "python")) {
    try {
        $exe = & cmd /c "$c -c ""import sys; print(sys.executable if sys.version_info >= (3, 8) else '')""" 2>$null
        if ($exe -and (Test-Path $exe.Trim())) { $py = $exe.Trim(); break }
    } catch {}
}
if (-not $py) { Write-Host "chatwatch needs Python 3.8 or newer. Install it from python.org (tick 'Add to PATH'), then run this again."; exit 1 }
# pythonw runs without a console window
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Here\chatwatch.py`" serve" -WorkingDirectory $Here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings `
    -Description "chatwatch: Claude Code usage per chat" | Out-Null
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 3
Write-Host "chatwatch is running: http://127.0.0.1:7731"
Start-Process "http://127.0.0.1:7731"
