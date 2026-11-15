# Remove the log-in auto start and stop the running bot server.
Stop-ScheduledTask -TaskName 'TradingBot' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'TradingBot' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'run\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Auto start removed and bot server stopped."
