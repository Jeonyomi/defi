Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*defi_agent*' } |
  Select-Object ProcessId,CreationDate,CommandLine | Format-List
