"""통합 검증 (전환 체크리스트 6번): main.py 기동 경로 + weth_usdc 무변경 회귀.

현재 라이브 설정(.env 그대로)으로 main.amain()과 동일한 구성 순서를 밟고
run_cycle 1회를 read-only로 태운다. 모든 쓰기 경로는 3중 차단:
- BaseClient.send 몽키패치 → 호출 즉시 예외 (온체인 tx 0건 보장)
- hedge.exchange = None (HL 주문 경로 차단, ensure_leverage는 호출 안 함)
- Store 대신 FakeStore (라이브 DB에 스냅샷/이벤트 오염 금지), tg 발송 없음

회귀 판정: 계산된 CycleReport를 라이브 봇의 직전 cycle 로그 수치와 대조.
(둘은 다른 시각의 관측이라 가격 드리프트 허용 오차를 둔다)
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# main.py의 basicConfig가 라이브 로그 파일(logs/agent.log)에 핸들러를 붙이기 전에
# 루트 로거를 콘솔로 선점 — 검증 출력이 라이브 로그에 섞이면 안 된다.
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


async def main():
    # ── 1) 기동 경로 임포트 (main 모듈 전체 임포트 그래프) ──
    import defi_agent.main as m  # noqa: F401
    check("main 임포트", True)

    from defi_agent.chain.base_client import BaseClient
    from defi_agent.config import load_settings
    from defi_agent.core.rebalancer import CycleReport, Rebalancer
    from defi_agent.hedge.hyperliquid_client import HyperliquidHedge
    from defi_agent.lp.aerodrome import AerodromeLP
    from defi_agent.tg.bot import TgInterface

    # ── 2) 쓰기 경로 차단 (몽키패치는 구성 전에) ──
    def _blocked_send(self, fn, value=0, gas_buffer=1.3):
        raise AssertionError(f"read-only 검증 중 send 호출됨: {fn.fn_name}")
    BaseClient.send = _blocked_send

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

    # ── 3) main.amain()과 동일한 구성 순서 (라이브 .env 그대로) ──
    s = load_settings()
    check("라이브 설정 lp_pair=weth_usdc", s.lp_pair == "weth_usdc", s.lp_pair)
    check("라이브 설정 tick_spacing=100", s.lp_tick_spacing == 100, str(s.lp_tick_spacing))

    client = BaseClient(s)
    lp = AerodromeLP(client)
    pool = lp.startup_verify()
    check("startup_verify 풀 해석", int(pool, 16) != 0, pool)

    hedge = HyperliquidHedge(s)
    hedge.exchange = None  # ensure_leverage는 건너뜀 — read-only 검증에 거래소 쓰기 금지
    store = FakeStore()

    rb = Rebalancer(s, lp, hedge, store)
    check("페어 플래그 price_is_usd", rb.price_is_usd is True)
    check("페어 플래그 full_delta", rb.full_delta is False)

    tg = TgInterface(s, rb, store)
    tg.notify = lambda text: sent_tg.append(text) or asyncio.sleep(0)  # 발송 차단

    # 액션은 기록만 하고 실행하지 않는다 (False 반환 → mint 후속 흐름도 진입 안 함)
    intents: list[str] = []

    async def _act_recorder(r, kind, desc, fn):
        intents.append(f"{kind}: {desc}")
        return False
    rb._act = _act_recorder

    # ── 4) read-only 사이클 실행 ──
    r = await rb.run_cycle()
    check("CycleReport 반환", isinstance(r, CycleReport))
    check("USD 페어: eth_usd == price", r.eth_usd == r.price,
          f"{r.eth_usd} vs {r.price}")
    check("가격이 USD 범위", 500 < r.price < 20000, f"{r.price:.2f}")
    check("equity > 0", r.equity > 0, f"{r.equity:.2f}")
    check("lp_value > 0 (라이브 포지션 존재)", r.lp_value > 0, f"{r.lp_value:.2f}")

    pos = lp.find_position()
    check("find_position", pos is not None)
    if pos:
        expected_delta = pos.amount0 + pos.owed0  # weth_usdc: token0만 (풀델타 아님)
        check("델타 = token0만 (구 산식)",
              abs(r.lp_delta - expected_delta) / max(expected_delta, 1e-9) < 0.01,
              f"{r.lp_delta:.6f} vs {expected_delta:.6f}")

    check("스냅샷 1건 (FakeStore)", len(store.snapshots) == 1)
    if store.snapshots:
        snap = store.snapshots[0]
        check("스냅샷 price=풀가", snap["price"] == r.price)
        check("스냅샷 mark_px > 0", snap["mark_px"] > 0, f"{snap['mark_px']:.2f}")
        if pos:
            check("스냅샷 lp_weth=amount0", snap["lp_weth"] == pos.amount0)

    print("의도된 액션(실행 안 함):", intents or "없음")
    check("tx 0건 (send 차단 비발동)", True)  # send 호출 시 위에서 예외로 즉사

    # ── 5) 라이브 봇 직전 사이클 로그와 회귀 대조 ──
    log_path = Path(__file__).resolve().parents[1] / "logs" / "agent.log"
    last = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        mm = re.search(r"cycle: equity=\$([\d.]+) lp=\$([\d.]+) delta=([\d.]+) "
                       r"hedge=([\d.]+)", line)
        if mm:
            last = tuple(float(g) for g in mm.groups())
    check("라이브 cycle 로그 존재", last is not None)
    if last:
        eq, lpv, dl, hg = last
        print(f"라이브 로그: equity={eq} lp={lpv} delta={dl} hedge={hg}")
        print(f"이번 계산 : equity={r.equity:.2f} lp={r.lp_value:.2f} "
              f"delta={r.lp_delta:.4f} hedge={r.hedge_size:.4f}")
        check("equity 회귀 (±3%)", abs(r.equity - eq) / eq < 0.03)
        check("lp_value 회귀 (±3%)", abs(r.lp_value - lpv) / lpv < 0.03)
        check("delta 회귀 (±5%)", abs(r.lp_delta - dl) / max(dl, 1e-9) < 0.05)
        check("hedge 회귀 (일치)", abs(r.hedge_size - hg) < 0.002)

    # ── 6) tg 렌더 경로 (발송 없이 실제 데이터로) ──
    txt = tg._status_text(r)
    check("상태 렌더", isinstance(txt, str) and len(txt) > 50)
    check("상태 렌더: USD 페어 가격 표기", "ETH $" in txt)
    check("상태 렌더: 페어 라벨", "WETH/USDC" in txt)
    check("상태 렌더: 비율 병기 없음(usd 페어)", "cbETH/WETH" not in txt)
    check("tg 발송 0건", len(sent_tg) == 0, str(len(sent_tg)))

    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print(f"통합 검증 전부 통과 — pool={pool}, tx 0건, DB 쓰기 0건, tg 발송 0건")


if __name__ == "__main__":
    asyncio.run(main())
