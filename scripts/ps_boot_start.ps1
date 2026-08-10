# defi-agent 부팅 자동 기동 래퍼 — 시작프로그램 defibot.vbs가 로그온 시 숨김 실행 (사용자 승인: 2026-07-30)
# 동작: 네트워크 안정화 대기 → 이미 실행 중이면 종료 → 기동 2회 시도 → 실패 시 TG 알림
param([int]$InitialDelay = 25)

$proj = 'C:\Users\USER\projects\defi-agent'
$log  = "$proj\logs\boot_start.log"

function Log($m) {
  Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
}

# wmic은 오탐이 있어 Get-CimInstance 사용 (운영 규칙)
function BotProc {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*defi_agent*' }
}

function Send-TG($text) {
  try {
    $envFile = 'C:\Users\USER\quant\.env'
    $tok  = (Select-String -Path $envFile -Pattern '^TELEGRAM_BOT_TOKEN=(.+)$').Matches[0].Groups[1].Value.Trim()
    $chat = (Select-String -Path $envFile -Pattern '^TELEGRAM_ALLOWED_USER_ID=(.+)$').Matches[0].Groups[1].Value.Trim()
    if (-not $tok -or -not $chat) { Log 'TG token/chat missing in quant/.env'; return }
    Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$tok/sendMessage" `
      -Body @{ chat_id = $chat; text = $text } -TimeoutSec 15 | Out-Null
    Log 'TG alert sent'
  } catch { Log ('TG send failed: ' + $_.Exception.Message) }
}

Log ("boot wrapper start (delay {0}s)" -f $InitialDelay)
Start-Sleep -Seconds $InitialDelay

if (BotProc) { Log 'already running - skip'; exit 0 }

for ($try = 1; $try -le 2; $try++) {
  Log ("start attempt {0}" -f $try)
  Start-Process -FilePath "$proj\.venv\Scripts\python.exe" `
    -ArgumentList '-m','defi_agent.main' -WorkingDirectory $proj -WindowStyle Hidden
  Start-Sleep -Seconds 45
  if (BotProc) { Log ("started OK (attempt {0})" -f $try); exit 0 }
}

Log 'start FAILED after 2 attempts'
Send-TG '[defi-agent] PC 부팅 후 봇 자동 시작이 2회 실패했습니다. 지금 봇이 꺼져 있는 상태라 직접 확인이 필요합니다.'
exit 1
