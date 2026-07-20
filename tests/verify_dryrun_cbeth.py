"""DRY_RUN 검증 (전환 체크리스트): DRY_RUN=true + cbeth_weth 설정 read-only 1사이클.

라이브 .env는 건드리지 않는다 — 프로세스 환경변수로만 전환값을 덮어쓴다
(config.load_dotenv는 override=False라 기존 os.environ이 항상 이긴다).
라이브 봇(weth_usdc)은 그대로 돌고, 이 스크립트는 별도 프로세스에서 eth_call만 수행.

검증 내용:
1. 전환 설정 로딩 (LP_PAIR=cbeth_weth, spacing=1, range=2%, max=110, DRY_RUN)
2. startup_verify가 문서의 실측 풀 주소를 해석 + token0=cbETH 정렬 확인
3. find_position이 기존 weth_usdc NFT를 페어 필터로 제외하는지 (오매칭=치명적)
4. run_cycle 1회: 비율 가격/마크 환산/풀델타 플래그/자동진입 보류 판단
5. mint 파라미터 예행 (read-only 수학): spacing=1 틱 정렬·±2% 레인지·50:50 근접
6. tx 0건 (send 호출 추적 + DRY_RUN 게이트), DB 쓰기 0건(FakeStore), tg 발송 0건

주의: 지갑 USDC(~$93)는 cbeth 모드 wallet_balances(cbETH/WETH만 조회)에 안 잡혀야
한다 — 전환 후 대기 자금 비침범 설계의 근거를 여기서 실측으로 확인한다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# ── 전환 설정 오버라이드 (load_settings 호출 전, .env 파일은 무수정) ──
os.environ.update({
    "DRY_RUN": "true",
    "LP_PAIR": "cbeth_weth",
    "LP_TICK_SPACING": "1",
    "LP_RANGE_PCT": "2",
    "LP_MAX_USDC": "110",
})

# 라이브 로그 파일에 핸들러가 붙기 전에 루트 로거를 콘솔로 선점
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DOC_POOL = "0x47cA96Ea59C13F72745928887f84C9F52C3D7348"  # 2026-07-20 실측 (문서 상단)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


async def main():
    import defi_agent.main as m  # noqa: F401 — 기동 경로 임포트 그래프
    from defi_agent import constants as C
    from defi_agent.chain.base_client import BaseClient
    from defi_agent.config import load_settings
    from defi_agent.core.rebalancer import CycleReport, Rebalancer
    from defi_agent.hedge.hyperliquid_client import HyperliquidHedge
    from defi_agent.lp import math as clmath
    from defi_agent.lp.aerodrome import AerodromeLP
    from defi_agent.tg.bot import TgInterface

    # ── 1) 설정 ──
    s = load_settings()
    check("DRY_RUN=true", s.dry_run is True)
    check("lp_pair=cbeth_weth", s.lp_pair == "cbeth_weth", s.lp_pair)
    check("tick_spacing=1", s.lp_tick_spacing == 1, str(s.lp_tick_spacing))
    check("range_pct=2", s.lp_range_pct == 2.0, str(s.lp_range_pct))
    check("lp_max_usdc=110", s.lp_max_usdc == 110.0, str(s.lp_max_usdc))

    # ── 2) send 추적 (DRY_RUN 게이트 위에 한 겹 더) ──
    send_calls: list[str] = []
    orig_send = BaseClient.send

    def _tracked_send(self, fn, value=0, gas_buffer=1.3):
        assert self.s.dry_run, "read-only 검증인데 dry_run=False 인스턴스에서 send 호출"
        send_calls.append(fn.fn_name)
        return orig_send(self, fn, value=value, gas_buffer=gas_buffer)
    BaseClient.send = _tracked_send

    sent_tg: list[str] = []

    class FakeStore:
        def __init__(self):
            self.snapshots: list[dict] = []
            self.events: list[tuple[str, str]] = []

        async def init(self): ...
        async def get_kv(self, key): return None
        async def set_kv(self, key, val): ...
        async def snapshot(self, **kw): self.snapshots.append(kw)
        async def log_event(self, kind, detail): self.events.append((kind, detail))
        async def equity_series(self, since_ts): return []
        async def edge_series(self, since_ts): return []
        async def recent_events(self, n=10): return []

    # ── 3) 구성 (main.amain()과 동일 순서) ──
    client = BaseClient(s)
    lp = AerodromeLP(client)
    pool = lp.startup_verify()
    check("풀 = 문서 실측 주소", pool.lower() == DOC_POOL.lower(), pool)
    check("token0 = cbETH 프리셋", lp.t0_addr.lower() == C.CBETH.lower())

    hedge = HyperliquidHedge(s)
    hedge.exchange = None  # HL 주문 경로 차단 (read-only)
    store = FakeStore()

    rb = Rebalancer(s, lp, hedge, store)
    check("price_is_usd=False", rb.price_is_usd is False)
    check("full_delta=True", rb.full_delta is True)
    check("pair_label", rb.pair_label == "cbETH/WETH", rb.pair_label)

    tg = TgInterface(s, rb, store)
    tg.notify = lambda text: sent_tg.append(text) or asyncio.sleep(0)

    # ── 4) 기존 weth_usdc NFT 오매칭 여부 (치명 조건) ──
    st = lp.pool_state()
    check("풀 가격 = 비율 (1.0~1.3)", 1.0 < st.price < 1.3, f"{st.price:.4f}")
    pos = lp.find_position()
    check("find_position=None (구 NFT 페어 필터 제외)", pos is None)
    # 같은 지갑에 구 포지션이 실제로 존재하는지 교차 확인 (필터가 일을 했다는 증거)
    n_nft = lp.npm.functions.balanceOf(client.address).call()
    check("지갑에 NFT 존재 (구 weth_usdc 포지션)", n_nft >= 1, f"{n_nft}개")

    bal0, bal1 = lp.wallet_balances()
    check("지갑 cbETH=0", bal0 == 0, f"{bal0}")
    check("지갑 WETH 소량 (USDC 비침범 — 잔고 조회에 USDC 없음)", bal1 < 0.01, f"{bal1:.5f}")

    # ── 5) read-only 사이클 ──
    r = await rb.run_cycle()
    check("CycleReport 반환", isinstance(r, CycleReport))
    check("price=비율", 1.0 < r.price < 1.3, f"{r.price:.4f}")
    check("eth_usd=HL 마크 (USD 범위)", 500 < r.eth_usd < 20000, f"{r.eth_usd:.2f}")
    check("eth_usd != price (비율/USD 분리)", r.eth_usd != r.price)
    check("lp_value=0 (신 풀 무포지션)", r.lp_value == 0)
    check("lp_delta=0", r.lp_delta == 0)
    check("hedge_size = 기존 숏 유지 관측", 0.03 < r.hedge_size < 0.08, f"{r.hedge_size:.4f}")
    check("wallet_usd < $100 (스왑 전 — USDC 미포함)", 0 < r.wallet_usd < 100,
          f"{r.wallet_usd:.2f}")
    check("자동진입 보류 (놀고 있음 알림)", any("놀고" in a for a in r.alerts),
          str(r.alerts))
    check("액션 0건", not r.actions, str(r.actions))
    check("send 0건 (tx 0건)", not send_calls, str(send_calls))
    check("스냅샷 1건 (FakeStore)", len(store.snapshots) == 1)
    if store.snapshots:
        snap = store.snapshots[0]
        check("스냅샷 price=비율", snap["price"] == r.price)
        check("스냅샷 mark_px=eth_usd", snap["mark_px"] == r.eth_usd)
    check("이벤트 0건", not store.events, str(store.events))

    # ── 6) mint 파라미터 예행 (read-only 수학 — tx 없음) ──
    eth_usd = r.eth_usd
    budget_t1 = 100 / eth_usd  # $100 → WETH 단위 (전환 실행 시 실제 예산 경로)
    lo = clmath.align_tick(clmath.price_to_tick(st.price * 0.98, 18, 18), 1)
    hi = clmath.align_tick(clmath.price_to_tick(st.price * 1.02, 18, 18), 1)
    check("틱 레인지 유효 (tl<tu)", lo < hi, f"{lo}..{hi}")
    check("현재 틱이 레인지 내", lo < st.tick < hi, f"tick={st.tick}")
    da0, da1, frac0 = clmath.amounts_for_budget(st.sqrt_price_x96, lo, hi,
                                                int(budget_t1 * 10**18))
    check("frac0 ≈ 50:50 (±2% 대칭 레인지)", 0.40 < frac0 < 0.60, f"{frac0:.3f}")
    total_t1 = (da0 / 10**18) * st.price + da1 / 10**18
    check("필요수량 합 ≈ 예산", abs(total_t1 - budget_t1) / budget_t1 < 0.02,
          f"{total_t1:.6f} vs {budget_t1:.6f} WETH")
    est_short = 100 / eth_usd  # full_delta 초기 헤지 추정 (est_frac=1.0)
    check("초기 헤지 추정 ≈ 0.05대 ETH", 0.02 < est_short < 0.09, f"{est_short:.4f}")

    # ── 7) tg 렌더 (발송 없음) ──
    txt = tg._status_text(r)
    check("상태 렌더", isinstance(txt, str) and len(txt) > 50)
    check("상태 렌더: 비율 병기", "cbETH/WETH" in txt)
    check("상태 렌더: ETH $ 병기", "ETH $" in txt)
    check("tg 발송 0건", len(sent_tg) == 0, str(len(sent_tg)))

    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print(f"DRY_RUN cbeth_weth 검증 전부 통과 — pool={pool}, "
          f"비율={r.price:.4f}, ETH=${r.eth_usd:.0f}, tx 0건, DB 쓰기 0건, tg 발송 0건")


if __name__ == "__main__":
    asyncio.run(main())
