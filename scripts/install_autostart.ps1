# Start the bot server automatically when you log in to Windows (no admin needed).
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
# Undo with scripts\remove_autostart.ps1. Trading stays stopped until you press Start.
$cmd = Join-Path $PSScriptRoot 'start_bot.cmd'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$cmd`"" -WorkingDirectory (Split-Path $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT1M'  # give IB Gateway a minute to start first
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'TradingBot' -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Trading bot server (dashboard + Telegram). Trading starts stopped.' -Force | Out-Null
Write-Host "Installed. The bot server will start a minute after you log in."
Write-Host "Start it now without logging out:  Start-ScheduledTask -TaskName TradingBot"
