# defi-agent — 델타뉴트럴 LP 자동화 에이전트

Base 체인 Aerodrome Slipstream **WETH/USDC** 풀에 LP를 공급하고, ETH 노출분을
**Hyperliquid 숏**으로 자동 헤지하는 시장중립 수익 에이전트.
수익 원천은 LP 수수료 + 펀딩 수취이며, 가격 방향 노출은 ~0%로 유지한다.

- 전략 근거(1년 실데이터 백테스트): [docs/DELTA_NEUTRAL_LP_BACKTEST.md](docs/DELTA_NEUTRAL_LP_BACKTEST.md)
- 전체 계획: [docs/DEFI_YIELD_AGENT_PLAN.md](docs/DEFI_YIELD_AGENT_PLAN.md)
- 방향성 전략과의 비교: [docs/STRATEGY_COMPARISON.md](docs/STRATEGY_COMPARISON.md)

## 동작 (10분 주기 사이클)

1. **신규 진입** — 지갑 USDC를 레인지 구성비대로 스왑 → 현재가 ±35% 레인지(집중도 m=6.0) mint → LP 델타만큼 HL 숏
2. **레인지 재배치** — 가격이 경계 90% 도달 시 청산→재중심화 (쿨다운 12h)
3. **재헤지** — LP 델타와 숏 수량 괴리 5% 초과 시 조정 (HL 최소주문 $15 이상일 때)
4. **수수료 수령** — 미수령 수수료 $50 초과 시 collect
5. **경보** — 펀딩 역전(-3% APR), 증거금 부족(실효 2.0x/2.5x), 에러 → 텔레그램 즉시 알림
6. **일일 리포트** — 매일 09:00 KST 자산·PnL·펀딩 요약 발송

## 구조

```
src/defi_agent/
├── config.py               # .env 로딩 (모든 한도의 단일 출처)
├── constants.py            # Base 주소 + 최소 ABI
├── keys.py                 # 프라이빗키 (Windows Credential Manager, DPAPI)
├── chain/base_client.py    # web3 연결, tx 전송, nonce 캐시, DRY_RUN 게이트
├── lp/math.py              # CL 수학 (tick/가격/정확한 수량 계산)
├── lp/aerodrome.py         # 풀 조회, mint/close/collect/swap
├── hedge/hyperliquid_client.py  # 숏 목표 수렴, 펀딩 조회, 레버리지 가드
├── core/rebalancer.py      # 전략 사이클 + 증거금 선확인 가드
├── core/state.py           # SQLite (이벤트 + 자산 스냅샷)
├── tg/bot.py               # 텔레그램 알림 (기본 알림 전용, 폴링 opt-in)
└── main.py                 # 오케스트레이터 (asyncio)
```

## 안전장치

| 장치 | 내용 |
|---|---|
| `DRY_RUN=true` 기본 | 모든 tx/주문을 시뮬레이션만 하고 전송 안 함 |
| `LP_MAX_USDC` 하드캡 | 에이전트가 이 이상 절대 예치 불가 |
| 증거금 선확인 | HL 증거금 < LP×0.5/레버리지×1.2 이면 진입 보류 (단방향 노출 방지) |
| 레버리지 가드 | 실효 레버리지가 `HL_MAX_LEVERAGE`(기본 3x) 초과하는 주문 거부 |
| 기동 시 검증 | factory→풀 해석, token0 확인, 컨트랙트 코드 존재 확인 |
| 키 보안 | 프라이빗키는 OS 키체인(DPAPI)에만. HL은 API wallet(출금 불가) |

---

# 새 PC 셋업 가이드 (clone → live)

## ⚠️ 시작 전 반드시

**에이전트는 전 세계에서 딱 1개만 실행되어야 한다.** 두 PC에서 동시에 돌리면
nonce 충돌 + 이중 헤지로 포지션이 깨진다. 새 PC로 옮길 때는 **기존 PC의
에이전트를 먼저 중지**할 것:

```powershell
# 기존 PC에서
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*defi_agent*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 0. 요구사항

- Windows 10/11 (키 저장이 Windows Credential Manager 기반. 리눅스/맥은 keyring
  백엔드가 다르지만 동일하게 동작 — SecretService/Keychain)
- Python 3.11+ 와 [uv](https://docs.astral.sh/uv/)
- 텔레그램 봇 토큰, Hyperliquid 계정 + 메인넷 API wallet

## 1. 클론 & 설치

```powershell
git clone https://github.com/Jeonyomi/defi.git
cd defi
uv venv
uv pip install -e .
```

## 2. 프라이빗키 등록 (파일에 저장 금지)

**기존 PC에서 키 내보내기** (이전하는 경우):

```powershell
# 기존 PC에서 실행 → 출력된 키를 안전하게 옮기고 화면·클립보드 삭제
.venv\Scripts\python -c "import keyring; print(keyring.get_password('defi-agent','wallet'))"
.venv\Scripts\python -c "import keyring; print(keyring.get_password('defi-agent','hl-api'))"
```

**새 PC에서 등록**:

```powershell
.venv\Scripts\python -m defi_agent.keys set wallet    # Base 운용 지갑 키
.venv\Scripts\python -m defi_agent.keys set hl-api    # HL API wallet 키
.venv\Scripts\python -m defi_agent.keys show          # 주소 확인 (키 미표시)
```

- 운용 지갑: 메인 지갑 금지. 운용 한도 자금만 입금 (USDC + 가스 ETH ~$10).
- HL API wallet: app.hyperliquid.xyz → More → API → Generate & **메인넷** Approve.
  유효기간이 있으니 만료 전 재승인 필요. 거래만 가능, 출금 불가.

## 3. .env 작성

```powershell
copy .env.example .env
```

필수 항목:

```ini
DRY_RUN=true                # 처음엔 반드시 true
LP_MAX_USDC=500             # LP 투입 하드캡
BASE_RPC=...                # 전용 RPC 권장 (Alchemy/QuickNode 무료 티어).
                            # 공용 mainnet.base.org는 429 잦음, base-rpc.publicnode.com은 차선
TG_BOT_TOKEN=...            # BotFather 토큰
TG_CHAT_ID=...              # 본인 chat_id
HL_ACCOUNT_ADDRESS=0x...    # HL 마스터(또는 서브계정) 주소 — 비밀 아님
HL_TESTNET=false
TG_POLLING=false            # 같은 토큰을 다른 봇이 폴링 중이면 반드시 false
```

선택 항목(기본값으로도 동작):

```ini
REBALANCE_INTERVAL_SEC=600  # 사이클 주기
STATUS_NOTIFY_MIN=60        # 정기 상태 알림 간격(분). 0이면 비활성.
                            # 사이클에 얹어 보내므로 실제 간격은 사이클 단위로 올림된다
                            # (예: 45분 설정 + 600초 사이클 → 실제 50분)
DAILY_REPORT_HOUR_KST=9     # 일일 리포트 발송 시각
```

참고: `~/quant/.env`가 존재하면 자동으로 함께 읽는다(프로젝트 .env가 우선).
없는 PC에서는 위 항목을 프로젝트 .env에 전부 채우면 된다.

## 4. 자금 배치

| 자금 | 위치 | 규모(L1 기준) |
|---|---|---|
| LP 원금 USDC | Base 네트워크 → 운용 지갑 | LP_MAX_USDC 만큼 |
| 가스 ETH | Base 네트워크 → 운용 지갑 | ~$10 |
| 헤지 증거금 USDC | Hyperliquid 입금 | LP의 30% 이상 |

⚠️ **HL 입금 주의**: CEX 직행 입금은 **스팟** 잔고로 들어간다.
앱에서 **Transfer → Spot to Perps**로 퍼프 증거금으로 옮겨야 에이전트가 인식한다.

⚠️ **진입 순서**: 에이전트는 (지갑 USDC ≥ $100) + (HL 퍼프 증거금 충분) 이 되는
순간 자동 진입한다. LP 자금을 나눠서 보낼 거면 HL 입금을 마지막에 할 것
(v0.1은 기존 포지션에 추가 자금 자동 편입이 없다).

## 5. DRY_RUN 관측 → 라이브

```powershell
# 포그라운드 실행 (로그 확인용)
.venv\Scripts\python -m defi_agent.main

# 백그라운드 실행 (터미널 닫아도 유지)
Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" `
  -ArgumentList '-m','defi_agent.main' -WorkingDirectory "$PWD" -WindowStyle Hidden
```

1. `DRY_RUN=true`로 최소 48시간: 사이클 로그(`logs\agent.log`)와 텔레그램 알림 확인
2. 이상 없으면 `.env`에서 `DRY_RUN=false` → 에이전트 재시작 (위 중지 명령 후 재실행)
3. 첫 진입 후 검증: 텔레그램 알림의 LP 델타와 숏 수량 일치 확인, 총자산 = 투입액 근사 확인
4. (선택) 작업 스케줄러에 로그온 시 자동 시작 등록

이력(PnL 스냅샷)을 이어가려면 기존 PC의 `defi_agent.db`를 프로젝트 루트로 복사.

## 운영 명령 모음

```powershell
# 로그 확인
Get-Content logs\agent.log -Tail 30
# 프로세스 확인
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*defi_agent*' }
# 재시작 = 중지(위 ⚠️ 명령) 후 백그라운드 실행
```

## 예산 로드맵

| 단계 | 조건 | LP | HL 증거금 | 비고 |
|---|---|---|---|---|
| L1 검증 | 시작 | $500 이하 | LP의 30%+ | 소액, 재헤지 정밀도 낮음(HL 최소주문 $15) |
| L2 확대 | 2주 무사고 | $2,000 | $600 | |
| L3 본운용 | +4주 무사고 | $5,000+ | LP의 30% | Safe 세션 키 도입 검토 |

## 알려진 한계 (v0.1)

- 단일 풀(WETH/USDC)·단일 포지션. Solana(Orca)·cbBTC 확장은 Phase 2.
- 기존 포지션에 추가 자금 자동 편입 없음 (재배치 시에도 LP 가치만 재예치).
- AERO 게이지 스테이킹 미지원.
- 텔레그램 명령(/status 등)은 폴링 opt-in — 토큰을 다른 봇과 공유 중이면 알림 전용.
- PnL은 총자산 스냅샷 기반 — 수수료/펀딩/IL 분해는 v0.2.
