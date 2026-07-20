# 전환 계획: WETH/USDC ±35% → cbETH/WETH ±2% LP + 풀 델타 숏 (권고안 ④)

승인: 2026-07-20 MJ ("권고안 4번으로 진행"). 근거: 리서치 루프 iter21 백테스트
(①현행 -17.1% / ②펀딩캐리 +7.4% / ③cbETH+숏 +9.6% / ④cbETH/WETH LP+숏 +10.3%,
디페그 -7% 스트레스에서도 ④ 연 +5.6% 양수).

이 문서는 세션이 끊겨도 이어갈 수 있도록 모든 확정 사실·결정·순서를 담는다.
**진행 상태는 맨 아래 체크리스트가 유일한 진실.**

## 확정된 온체인 사실 (2026-07-20 실측)

- 대상 풀: Aerodrome Slipstream cbETH/WETH, **tickSpacing=1**
  - pool = `0x47cA96Ea59C13F72745928887f84C9F52C3D7348`
  - token0 = cbETH `0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22` (18dp)
  - token1 = WETH `0x4200000000000000000000000000000000000006` (18dp)
  - fee=70 (0.007%), tick=1269, 비율 1 cbETH = 1.1353 WETH, 유동성 충분 (liq≈1.83e25)
- factory/NPM/SwapRouter는 기존 constants.py 주소 그대로 사용 가능 (같은 Slipstream).
- 현재 포지션 (스냅샷 1784520930): LP 0.04564 WETH + 108.31 USDC ≈ $193.7,
  미수령 수수료 ≈ $0.21, HL 숏 0.0504 ETH @ 증거금 $61.39, 지갑 WETH 0.00209, 총 $259.2.

## 자본배분 결정 (레버리지 산식이 바뀐다)

새 구조는 LP 전액이 ETH 델타 → 숏 노셔널 = LP 가치 (구조상 기존의 ~2배).
봇 예산 규칙(HL 증거금 ≥ 노셔널 60%, 즉 lev ≤ 1.67x)을 유지하기 위해:

- **1차 전개: LP = $100** → 숏 ≈ 0.0535 ETH, 레버리지 ≈ 1.63x ✅
- 나머지 ≈ $93은 Base 지갑 USDC로 대기 (cbeth_weth 모드 봇은 USDC를 안 건드림 → 자동진입 안전)
- `LP_MAX_USDC=110`으로 봇의 자동 재진입 상한도 고정
- 확장 옵션(사용자 액션): HL에 USDC ~$40 입금 시 LP를 ~$155까지 증액 가능 (lev 1.55x 유지)

## .env 전환값 (전환 시점에 변경)

```
LP_PAIR=cbeth_weth      # 신규 키
LP_PAIR_SINCE=<전환 tx unix ts>  # 신규 키 — analytics 관측 창 시작 강제 (이전 스냅샷 혼입 차단)
LP_TICK_SPACING=1       # 기존 100
LP_RANGE_PCT=2          # 기존 35
LP_MAX_USDC=110         # 기존 500
```
HL_COIN=ETH 유지 (cbETH 노출을 ETH 숏으로 헤지 — 베이시스=cbETH/ETH 비율이며 그게 곧 LP 레인지).

## 코드 일반화 범위 (LP_PAIR 설정 기반)

WETH/USDC 하드코딩을 페어 설정으로 치환. cbeth_weth 모드의 의미 변화:

| 항목 | weth_usdc (기존) | cbeth_weth (신규) |
|---|---|---|
| pool price | USDC per WETH = USD가 | WETH per cbETH = 비율 (~1.135) |
| USD 환산 | st.price | **hs.mark_px (HL ETH 마크)** |
| lp_delta (ETH) | weth_amount+owed | **cbeth×ratio + weth (+owed 양쪽)** |
| 헤지 타깃 | token0 수량 | **풀 델타 전체** |
| mint 예산 | usd_total→USDC(6dp) raw | usd_total/eth_usd→WETH(18dp) raw |
| 초기 헤지 추정치 | deployable×0.42/price | **deployable/eth_usd (×1.0)** |
| analytics 변동성 | ETH/USD 가격 시계열 | 비율 시계열 (자연히 올바름) |

터치포인트: constants.py(CBETH 추가), config.py(LP_PAIR), lp/aerodrome.py(token0/1
파라미터화, prepare_ratio/mint_centered/swap), core/rebalancer.py(델타·환산·진입로직),
core/analytics.py(USD 환산 경로), tg 메시지(표기), core/state.py(스냅샷 컬럼은
lp_weth/lp_usdc 이름 유지하되 cbeth 모드에선 t0/t1 의미 — 주석으로 명시, 스키마 변경 금지).
snapshots에 mark_px 이미 있음 → analytics USD 환산에 사용.

주의: 모드 전환 시 analytics의 관측 창은 price 의미가 바뀌므로 **전환 시점 이전
스냅샷과 섞어 계산하면 안 됨** — 전환 tx 이후 ts만 쓰도록 창 시작점을 강제할 것.

## 전환 실행 순서 (각 단계 델타 갭 ≤ $20 유지)

0. 사이클 틈 확인 후 봇 정지 (Get-CimInstance로 프로세스 확인 — wmic 금지,
   래퍼까지 두 프로세스 모두 정지).
1. (코드 완료 + DRY_RUN 검증 통과가 선행 조건)
2. 구 LP 청산: close_position (weth_usdc 모드 스크립트) → 지갑 ≈ 0.0477 WETH + $195 USDC.
   숏 0.0504 유지 (지갑 WETH가 델타 커버) ✅
3. HL 숏 타깃 0.0535로 조정 (+0.0031, ~$6 — $15 미만이면 스킵하고 5단계 후 봇에 맡김).
4. 스왑 (구 풀 spacing=100): USDC→WETH로 지갑 WETH를 0.0535 ETH-eq까지 증액.
   그 다음 신 풀(spacing=1)에서 WETH→cbETH ≈ 절반 (mint 비율은 봇 prepare_ratio가 정밀 조정).
5. .env 전환값 적용 → 봇 재기동 → 자동진입 분기가 cbETH/WETH mint + 풀 델타 헤지 수행.
6. 검증: 첫 사이클 로그 — LP ≈ $100, 숏 ≈ 0.0535, lev ≤ 1.7x, 잔여 USDC ≈ $93 비침범.
   30분 뒤 두 번째 사이클에서 드리프트/알림 정상 확인.

롤백: 4단계 이전 실패 시 — 구 모드 그대로 재기동하면 자동진입이 WETH/USDC 재구성.
5단계 이후 실패 시 — cbETH/WETH는 동종자산이라 held 상태로도 델타는 숏이 커버, 급할 것 없음.

## 증액 전개 (2026-07-20 MJ 승인 "충전분 최대한 활용해서 라이브 업데이트")

2026-07-20 19:15 실측:
- Base 지갑 USDC **+$405 도착 확인** → 잔고 $500.43 (가스 ETH 0.0100 충분)
- HL 충전분 **$185.58이 Arbitrum 체인의 지갑 주소(0xa9a2…338b)에 정체** — HL 브리지로
  미입금 상태라 HL 증거금은 여전히 $61.7. 이 지갑의 마스터 키는 봇에 없음(보유 키:
  Base wallet, HL API 지갑 — API 지갑은 자금 이동 불가) → **사용자만 완결 가능**
  (app.hyperliquid.xyz 접속 → Deposit 클릭. USDC는 이미 올바른 체인·지갑에 있음).
- 완결 시 HL 증거금 ≈ $247 예상.

목표 산식 (입금액이 달라져도 이 식으로 재계산):
`TARGET_LP_USD = floor(min(1.6 × HL증거금, 현 LP가치 + 지갑 USDC − 10))`
— HL $247 기준 ≈ **$395** (lev 1.6x ≤ 1.67 규칙 내, 숏 ≈ 0.212 ETH,
HL 마진사용 = 노셔널/3 ≈ $132 < 증거금). 전개 후 대기 USDC ≈ $207.
전액($602) 전개하려면 HL 증거금 ≥ $376 필요 (추가 ~$130 입금 시 — 사용자 옵션).

실행 순서 (각 단계 후 온체인 실측, 델타 갭 ≤ $25 유지):
0. 게이트: `migrate_cbeth_weth.py status`로 HL 증거금 ≥ $240 확인. 미만이면 대기.
1. 사이클 틈 봇 정지 (ps_stop_bot.ps1, Get-CimInstance로 잔존 확인)
2. `close-cbeth` — 현 LP #73317233 청산 (cbETH+WETH 지갑 회수 — 숏 0.0504가 커버하는
   델타는 변하지 않음, 수수료 수거 포함)
3. `TARGET_LP_USD=<산식값> … swap-to-weth` — USDC→WETH 부족분 매입 (보유 cbETH 포함 셈)
4. `swap-to-cbeth` — ±2% mint 구성비(frac0)로 정렬
5. .env `LP_MAX_USDC=<TARGET+15>` 수정 → 봇 재기동 → 자동진입이 mint + 초기 헤지로
   숏을 풀 델타(~0.212)로 증액 (조정분 ~$290 ≫ 최소주문 $15라 즉시 실행됨)
6. 검증: LP ≈ TARGET, 숏 ≈ LP델타, lev ≤ 1.67, 두 사이클 연속 정상, 대기 USDC 실측
   (LP_PAIR_SINCE는 유지 — 페어 동일. analytics 창은 청산 시 수수료 수거 감지로 자연 리셋)
롤백: mint 이전 실패 시 그대로 재기동하면 자동진입이 재구성 (지갑 자산은 숏이 델타 커버).

## 진행 체크리스트 (완료 시 [x] + 타임스탬프 기입)

- [x] 풀 온체인 검증 (2026-07-20, 이 문서 상단)
- [x] 코드 일반화 + 커밋 (라이브 무영향: LP_PAIR 기본값 weth_usdc) — 2026-07-20 16:20, 하위 6항목 전부 완료
  - [x] 1. constants(CBETH·LP_PAIRS) + config(LP_PAIR 검증) — 2026-07-20 13:35, 단위검증 통과
  - [x] 2. lp/aerodrome.py 파라미터화 — 2026-07-20 13:58, tests/verify_lp_pair_params.py 17건 통과
    (Position 필드 amount0/1·owed0/1로 일반화 + weth_amount 등 호환 별칭 유지 → rebalancer 무수정,
    mint_centered는 budget_t1(token1 단위) + usd_value(상한 체크용) 시그니처 — cbeth 모드 호출부는 3번에서)
  - [x] 3. core/rebalancer.py — 2026-07-20, tests/verify_rebalancer_pair.py 26건 통과
    (lp_delta=_pos_delta 풀델타, USD환산 to_usd=mark_px, 초기헤지 est_frac 1.0, need_margin
    delta_frac 0.5→1.0, mint/rerange 예산 token1 단위 + usd_value, mark_px=0 관측전용 가드,
    스냅샷은 amount0/1 원시량 저장 — weth_usdc 구 산식 수치 일치 회귀 포함)
  - [x] 4. core/analytics.py — 2026-07-20, tests/verify_analytics_pair.py 19건 통과
    (compute_edge에 price_is_usd 파라미터: cbeth 모드는 token1(WETH) 단위로 셈한 뒤
    창 내 마지막 유효 mark_px로만 USD 환산(행별 마크 혼용 금지 — ETH/USD 변동이 IL로
    둔갑 방지), mark 전무 시 None. sigma는 vol_ref(HL ETH vol) 무시하고 비율 시계열
    실측(vol_src="ratio"). 창 시작 강제 2중: config LP_PAIR_SINCE(신규, 기본 0) +
    tg/_edge since=max(now-7d, since) 1차, compute_edge 내 인접 price 50% 급변 절단
    2차. weth_usdc는 구 산식 수치 일치 회귀 확인, tg 표기는 5번에서)
  - [x] 5. tg 표기 — 2026-07-20, tests/verify_tg_pair.py 22건 통과
    (CycleReport.eth_usd 신설(USD 환산가), LP_PAIRS에 label, 상태/액션 가격 표기
    cbeth 모드는 "cbETH/WETH 1.1353 · ETH $X" 병기, 헤지 빈틈 $ 환산 eth_usd로
    수정(비율×수량 버그 예방), edge 문구 "비율 출렁임"·vol_src=ratio는 정규
    소스라 불안정 주석 제외, LP 줄에 페어 라벨. weth_usdc 수치 회귀 확인.
    부수: verify 스크립트들 cp949 콘솔 인코딩 보호(utf-8 reconfigure))
  - [x] 6. 통합 — 2026-07-20 16:20, tests/verify_integration_readonly.py 30건 통과
    (main.py 무수정으로 새 시그니처와 정합 확인. 라이브 .env(weth_usdc) 그대로 amain()과
    동일한 구성 순서 + run_cycle 1회를 read-only로 실행: send 몽키패치·exchange=None·
    FakeStore 3중 차단으로 tx/DB 쓰기/tg 발송 0건. 결과를 라이브 봇 16:16 사이클 로그와
    대조 — equity 259.27 vs 259.28, lp 192.96 vs 193.02, delta 0.0475 vs 0.0474,
    hedge 0.0504 일치. tg 상태 렌더도 실데이터로 통과)
- [x] DRY_RUN=true + cbeth_weth 설정으로 read-only 1사이클 검증 (tx 0건) — 2026-07-20,
  tests/verify_dryrun_cbeth.py 40건 통과 (라이브 .env 무수정 — 프로세스 env 오버라이드만.
  풀=문서 실측 주소 해석, 비율 1.1353, find_position이 구 weth_usdc NFT를 페어 필터로
  제외(오매칭 없음), eth_usd=HL 마크 $1864, 지갑 잔고 조회에 USDC 미포함(대기 $93
  비침범 실증), 자동진입은 "지갑 $4 < $100"으로 정상 보류, mint 예행 수학 spacing=1
  틱 1067..1467에 현재틱 1269 포함·frac0=0.494·필요수량 합=예산 일치, 초기 헤지
  추정 0.0537 ETH ≈ 계획 0.0535. tx/DB/tg 전부 0건 — 라이브 전환(0~6) 선행 조건 충족)
- [x] 라이브 전환 실행 (위 0~6) — 2026-07-20 17:33 완료 (하위 전 항목 [x]) — 실행 도구: scripts/migrate_cbeth_weth.py (status/close/swap-to-weth/swap-to-cbeth) + scripts/ps_stop_bot.ps1·ps_start_bot.ps1
  - [x] 0. 사이클 틈 봇 정지 — 2026-07-20 17:31 (직전 사이클 17:26:20 완료 확인,
    PID 14568/35708 두 프로세스 정지, 잔존 없음 Get-CimInstance 확인)
  - [x] 2. 구 LP 청산 — 17:32, #73154313 decrease/collect/burn 3tx 확정, NFT 소각 실측.
    청산 후 지갑 0.048519 WETH + 107.07 USDC. 숏 0.0504 유지, 델타 갭 $3.5
  - [x] 3. HL 숏 조정 — 스킵 판정: 조정분 0.0031 ETH ≈ $5.8 < $15 (문서 규정대로 5단계 후 봇 위임)
  - [x] 4a. USDC→WETH 스왑 — 17:32, 0.006248 WETH 매입 → 지갑 0.054766 WETH(=$102 @ $1862.43),
    대기 USDC 95.43. 델타 갭 $8.1
  - [x] 4b. WETH→cbETH 스왑 — 17:33, 신 풀 ticks[1067,1467] frac0=0.4930 →
    0.027764 WETH + 0.023781 cbETH, ETH-eq 0.054764 보존, USDC 무변동
  - [x] 5. .env 전환값 적용 + 봇 재기동 — 17:33:00, LP_PAIR=cbeth_weth,
    LP_PAIR_SINCE=1784536377, LP_TICK_SPACING=1, LP_RANGE_PCT=2, LP_MAX_USDC=110
  - [x] 6a. 첫 사이클 검증 — 17:33:11 mint tx 확정(47024b5a…), 온체인 실측:
    LP #73317233 cbETH 0.023781 + WETH 0.027762, ticks[1067,1467] 중앙(현재틱 1269),
    LP 델타 0.0548 = $101.99, 숏 0.0504 (갭 $8.1 — HL 최소주문 $15 미만이라 봇이 수렴 대기,
    ≤$20 규율 내), lev 1.65x, 대기 USDC 95.43 비침범, 지갑 더스트 0.
    첫 사이클 로그 lp=$0.00은 리포트 수치가 mint 이전 조회분이기 때문 — 다음 사이클부터 정상 표기
  - [x] 6b. 후속 사이클 검증 — 2026-07-20 17:53 (사이클 주기는 실제 10분: 17:43·17:53
    두 사이클 연속 정상. lp=$101.72→$101.91, delta=0.0548 유지, hedge=0.0504,
    드리프트 8.0% → 재헤지 보류 "$8 < $15" 분기 정상 작동(≤$20 규율 내), range=1%,
    재기동 후 ERROR/WARN 0건, TG 알림 전용 모드 정상. equity $163.88은 대기 USDC
    $95.43 미포함 값 — 합산 $259.31로 전환 전 총자산과 일치(자금 누수 없음))
- [x] 증액 전개 — 2026-07-20 19:09 완료 (게이트: 사용자가 HL Deposit 완결 → 증거금 $247.19 실측)
  - 0~4단계 (19:03~19:05): 봇 정지 → 구 $102 LP #73317233 청산 → swap-to-weth →
    swap-to-cbeth. 결과: 지갑 WETH 0.107456 + cbETH 0.092081 (ETH-eq 0.2120 ≈ $395
    = 산식 1.6×$247), 대기 USDC $207.45. ※ 이 구간 직후 세션 절단 — 다음 턴에서 재개
  - 5단계 (19:09): .env LP_MAX_USDC=110→410, 봇 재기동 19:09:08. 자동진입이
    mint tx `b37862dd…` 확정 + HL 숏 +0.1616 ETH 체결(@$1864.9, oid 499233217331)
  - 6단계 검증 (19:11 온체인 실측): 신 LP #73319755 cbETH 0.092081 + WETH 0.107451
    (ETH-eq 0.2120), 숏 0.2120 → **델타 갭 $0.0**, lev 1.60x ≤ 1.67, 대기 USDC
    $207.45 비침범, 지갑 더스트 WETH 0.000005뿐. 총자산 ≈ LP $395 + HL $247 + 대기 $207
- [ ] 전환 후 24h 관측: 수수료 적재·비율 변동성·재헤지 빈도 확인, MJ에게 결과 보고
  (증액 전개 완료 시점 2026-07-20 19:09부터 24h 기산 → 판정 7-21 19:00 이후)
