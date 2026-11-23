# chatwatch tray icon for Windows: the plan limit you are closest to, by the clock.
# install.ps1 starts it at sign-in. To run it by hand:
#   powershell -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File .\windows\tray.ps1
# It only reads http://127.0.0.1:7731/api/chats; the chatwatch service does the work and the toasts.
# UNTESTED on Windows so far. If something fails, please send the error text back.
$ErrorActionPreference = "Stop"
$Base = "http://127.0.0.1:7731"

Add-Type -AssemblyName System.Windows.Forms, System.Drawing, Microsoft.VisualBasic
Add-Type -Namespace Chatwatch -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool DestroyIcon(System.IntPtr handle);
'@

# one tray icon per user, even if the task starts twice
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\chatwatch-tray", [ref]$created)
if (-not $created) { exit 0 }

function Get-LightTaskbar {
    try {
        $v = Get-ItemPropertyValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" SystemUsesLightTheme
        return $v -eq 1
    } catch { return $false }
}

# A ring filled to the meter's percentage with the number inside, red when the limit is
# near (same rule as Model.warning in mac/Chatwatch.swift), and a dot when chats need you.
function New-TrayIcon([object]$pct, [bool]$warning, [int]$urgent, [bool]$down) {
    $size = [System.Windows.Forms.SystemInformation]::SmallIconSize.Width
    if ($size -lt 16) { $size = 16 }
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    $ink = if (Get-LightTaskbar) { [System.Drawing.Color]::Black } else { [System.Drawing.Color]::White }
    if ($warning) { $ink = [System.Drawing.Color]::FromArgb(232, 59, 59) }
    $faint = [System.Drawing.Color]::FromArgb(90, $ink)

    $w = [Math]::Max(1.5, $size / 10.0)
    $r = New-Object System.Drawing.RectangleF(($w / 2), ($w / 2), ($size - $w), ($size - $w))
    $track = New-Object System.Drawing.Pen($faint, $w)
    $g.DrawEllipse($track, $r)
    if ($null -ne $pct -and $pct -gt 0) {
        $arc = New-Object System.Drawing.Pen($ink, $w)
        $g.DrawArc($arc, $r, -90, [float](360 * [Math]::Min(100, $pct) / 100))
        $arc.Dispose()
    }
    $track.Dispose()

    $label = if ($down) { "?" } elseif ($null -eq $pct) { "" } elseif ($pct -ge 99.5) { "!" } else { [string][int][Math]::Round($pct) }
    if ($label) {
        $fontPx = if ($label.Length -ge 2) { $size * 0.48 } else { $size * 0.6 }
        $font = New-Object System.Drawing.Font("Segoe UI", [float]$fontPx, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
        $fmt = New-Object System.Drawing.StringFormat
        $fmt.Alignment = [System.Drawing.StringAlignment]::Center
        $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
        $brush = New-Object System.Drawing.SolidBrush($ink)
        $g.DrawString($label, $font, $brush, (New-Object System.Drawing.RectangleF(0, 0, $size, $size)), $fmt)
        $brush.Dispose(); $font.Dispose(); $fmt.Dispose()
    }
    if ($urgent -gt 0) {
        $d = [Math]::Max(5, $size * 0.36)
        $dot = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(245, 158, 11))
        $g.FillEllipse($dot, ($size - $d), 0, $d, $d)
        $dot.Dispose()
    }
    $g.Dispose()
    $h = $bmp.GetHicon()
    $bmp.Dispose()
    return @{ Icon = [System.Drawing.Icon]::FromHandle($h); Handle = $h }
}

$tray = New-Object System.Windows.Forms.NotifyIcon
$script:handle = [IntPtr]::Zero

function Set-Tray([object]$pct, [bool]$warning, [int]$urgent, [bool]$down, [string]$tip) {
    $made = New-TrayIcon $pct $warning $urgent $down
    $tray.Icon = $made.Icon
    if ($script:handle -ne [IntPtr]::Zero) { [Chatwatch.Native]::DestroyIcon($script:handle) | Out-Null }
    $script:handle = $made.Handle
    # NotifyIcon.Text takes at most 63 characters
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 62) + "." }
    $tray.Text = $tip
}

function Update-Tray {
    try {
        $d = Invoke-RestMethod -Uri "$Base/api/chats" -TimeoutSec 5
    } catch {
        Set-Tray $null $false 0 $true "chatwatch is not running. Run install.ps1 again."
        $timer.Interval = 10000
        return
    }
    $timer.Interval = 30000

    # the meter per-chat shares are counted against, or the fullest one
    $meter = $null
    $plan = $d.plan
    if ($plan -and $plan.meters) {
        $all = @($plan.meters.PSObject.Properties | ForEach-Object { $_.Value })
        if ($plan.unit -and $plan.meters.($plan.unit)) { $meter = $plan.meters.($plan.unit) }
        elseif ($all.Count) { $meter = $all | Sort-Object pct -Descending | Select-Object -First 1 }
    }
    $urgent = @($d.alerts | Where-Object { $_.notify }).Count

    $pct = $null; $warning = $false; $tip = "Plan limits unavailable"
    if ($meter) {
        $pct = [double]$meter.pct
        $warning = $pct -ge 90 -or ($meter.full_at -and $meter.resets -and $meter.full_at -lt $meter.resets)
        $name = [string]$meter.label
        if ($name) { $name = $name.Substring(0, 1).ToUpper() + $name.Substring(1) }
        $tip = "{0}: {1:0}%" -f $name, $pct
        if ($meter.full_at -and $meter.resets -and $meter.full_at -lt $meter.resets) { $tip += ", full before reset" }
    }
    if ($urgent -eq 1) { $tip += "`n1 chat needs you" }
    elseif ($urgent -gt 1) { $tip += "`n$urgent chats need you" }
    Set-Tray $pct $warning $urgent $false $tip
}

# The page as a small app window by the tray; bring it forward if it is already open.
function Open-Panel {
    try { [Microsoft.VisualBasic.Interaction]::AppActivate("Chat usage"); return } catch {}
    $area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $w = 420; $h = [Math]::Min(820, $area.Height - 24)
    $x = $area.Right - $w - 12; $y = $area.Bottom - $h - 12
    $edgeDir = Join-Path $env:LOCALAPPDATA "chatwatch\edge"
    try {
        Start-Process "msedge" -ArgumentList @("--app=$Base", "--window-size=$w,$h", "--window-position=$x,$y",
            "--user-data-dir=`"$edgeDir`"", "--no-first-run")
    } catch {
        Start-Process $Base
    }
}

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$menu.Items.Add("Open dashboard", $null, { Start-Process $Base }) | Out-Null
$menu.Items.Add("Refresh", $null, { Update-Tray }) | Out-Null
$menu.Items.Add("-") | Out-Null
$menu.Items.Add("Quit", $null, { $timer.Stop(); $tray.Visible = $false; [System.Windows.Forms.Application]::Exit() }) | Out-Null
$tray.ContextMenuStrip = $menu
$tray.add_MouseClick({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Open-Panel }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30000
$timer.add_Tick({ Update-Tray })

Set-Tray $null $false 0 $true "chatwatch is starting"
$tray.Visible = $true
Update-Tray
$timer.Start()
[System.Windows.Forms.Application]::Run()

$tray.Dispose()
if ($script:handle -ne [IntPtr]::Zero) { [Chatwatch.Native]::DestroyIcon($script:handle) | Out-Null }
$mutex.ReleaseMutex()
