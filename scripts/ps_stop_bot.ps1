Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*defi_agent*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output "stopped $($_.ProcessId)" }
