$proj = 'C:\Users\USER\projects\defi-agent'
Start-Process -FilePath "$proj\.venv\Scripts\python.exe" `
  -ArgumentList '-m','defi_agent.main' -WorkingDirectory "$proj" -WindowStyle Hidden
Write-Output 'started'
