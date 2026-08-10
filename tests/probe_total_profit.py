"""누적손익(Total Profit) 표기 검증 — 렌더된 알림 본문으로 판정한다.

숫자가 '맞게 계산됐다'가 아니라 '사용자가 받는 문장에 실제로 박혔다'까지 본다.
원장을 바꿨을 때 표기가 따라 변하는지(변이 검사)도 함께 확인한다 —
고정 문자열을 뱉는 코드도 정상 케이스만 보면 통과하기 때문이다.

실행: .venv/Scripts/python.exe tests/probe_total_profit.py
DB는 임시 파일에 만든다 (운용 DB 무변경).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from defi_agent.core.rebalancer import CycleReport  # noqa: E402
from defi_agent.core.state import Store  # noqa: E402
from defi_agent.tg.bot import TgInterface  # noqa: E402

OK = FAIL = 0


def check(name: str, cond: bool, got=None):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"\n        got: {got!r}" if got is not None else ""))


class FakeSettings:
    dry_run = False
    hold_mode = True
    rerange_trigger = 0.90
    tg_bot_token = ""
    tg_chat_id = ""


class FakeRb:
    paused = False
    price_is_usd = False
    pair_label = "cbETH/WETH"


def report(equity: float) -> CycleReport:
    """2026-08-10 13:00 KST 실제 알림과 같은 수치."""
    return CycleReport(
        ts=time.time(), price=1.1367, eth_usd=1916.35, equity=equity,
        lp_value=0.0, hedge_size=0.3206, lp_delta=0.3210, funding_apr=10.9,
        hl_account=233.45, hedge_upnl=-13.99, wallet_usd=615.21, eff_lev=2.63)


def tg(store: Store) -> TgInterface:
    t = TgInterface.__new__(TgInterface)  # __init__은 aiogram Bot을 만든다 — 우회
    t.s, t.rb, t.store = FakeSettings(), FakeRb(), store
    t._vol_cache = None
    t.bot = None
    t.last_report = None
    t._alert_last = {}
    t._status_last = 0.0
    return t


async def main():
    db = Path(tempfile.mkdtemp()) / "probe.db"
    store = Store(str(db))
    await store.init()
    t = tg(store)
    r = report(848.66)

    print("\n[1] 원장이 비었을 때 — 총자산을 수익으로 둔갑시키지 않는가")
    check("_profit()가 None", await t._profit(r.equity) is None)
    body = t._status_text(r, profit=await t._profit(r.equity))
    check("본문에 '미집계' 안내", "누적손익 _미집계" in body, body)
    check("848.66을 손익으로 쓰지 않음", "누적손익 +$848" not in body, body)

    print("\n[2] 실제 원장(투입 $850.04) — 손익이 본문에 박히는가")
    seed = [(1784017, 8.80), (1784204, 200.00), (1784215, 50.86),
            (1784577, 405.00), (1784578, 185.38)]
    base = int(time.time()) - 27 * 86400
    for i, (_, usd) in enumerate(seed):
        await store.add_flow(base + i * 60, usd, f"seed{i}")
    p = await t._profit(r.equity)
    pnl, pct, invested, days = p
    check("투입원금 850.04", abs(invested - 850.04) < 0.005, invested)
    check("손익 -1.38", abs(pnl - (848.66 - 850.04)) < 0.005, pnl)
    check("수익률 -0.16%", abs(pct - (848.66 / 850.04 - 1) * 100) < 0.001, pct)
    check("운용일수 ~27", 26.9 < days < 27.1, days)

    body = t._status_text(r, profit=p)
    print("\n".join("      " + x for x in body.splitlines()))
    check("손익 줄이 본문에", "누적손익 -$1.38" in body, body)
    check("투입원금 표기", "투입 $850.04" in body, body)
    check("구성 줄이 ├ 로 바뀜", "├ cbETH 보유 $615.21" in body, body)
    check("총자산 줄 유지", "총자산 $848.66" in body, body)
    check("트리 마지막이 손익", body.count("└ *누적손익") == 1, body)

    print("\n[3] 변이 검사 — 원장을 바꾸면 표기가 따라 움직이는가")
    await store.add_flow(base + 999, 100.0, "추가 입금")
    p2 = await t._profit(r.equity)
    check("투입 850.04 → 950.04", abs(p2[2] - 950.04) < 0.005, p2[2])
    check("손익이 -1.38 → -101.38", abs(p2[0] + 101.38) < 0.005, p2[0])
    b2 = t._status_text(r, profit=p2)
    check("본문 숫자도 변함", "누적손익 -$101.38" in b2 and "-$1.38" not in b2, b2)
    check("입금은 수익이 아니다 (손익 감소)", p2[0] < pnl, (p2[0], pnl))
    await store.del_flow(base + 999)

    print("\n[4] 출금(음수) 처리")
    await store.add_flow(base + 998, -200.0, "출금")
    p3 = await t._profit(r.equity)
    check("투입 850.04 → 650.04", abs(p3[2] - 650.04) < 0.005, p3[2])
    check("출금하면 손익 +198.62", abs(p3[0] - (848.66 - 650.04)) < 0.005, p3[0])
    await store.del_flow(base + 998)

    print("\n[5] 이익 국면 부호 표기")
    p4 = await t._profit(1000.0)
    b4 = t._status_text(report(1000.0), profit=p4)
    check("+$149.96 로 표기", "누적손익 +$149.96" in b4, b4)
    check("퍼센트에 + 부호", "(+17.64%)" in b4, b4)

    print("\n[6] /pnl 본문")
    t.last_report = r
    txt = await t._pnl_text()
    print("\n".join("      " + x for x in txt.splitlines()))
    check("누적손익 머리글", "누적손익 -$1.38" in txt, txt)
    check("투입→현재 표기", "투입 $850.04 → 현재 $848.66" in txt, txt)
    check("연환산 포함(27일)", "연환산" in txt, txt)
    check("스냅샷 없어도 죽지 않음", "스냅샷 부족" in txt or "관측" in txt, txt)

    print("\n[7] LP 모드(hold_mode=False)에서도 손익 줄 유지")
    t.s.hold_mode = False
    r2 = report(848.66)
    r2.lp_value = 400.0
    b7 = t._status_text(r2, profit=await t._profit(r2.equity))
    check("LP 줄이 ├", "├ LP cbETH/WETH" in b7, b7)
    check("손익 줄 존재", "└ *누적손익 -$1.38*" in b7, b7)

    print(f"\n{'=' * 46}\n  PASS {OK} · FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
