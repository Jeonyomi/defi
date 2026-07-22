"""tg/bot.py 페어 인지 표기 오프라인 단위검증 (체인·HL·텔레그램 접속 없음, tx 없음).

실행: .venv/Scripts/python tests/verify_tg_pair.py
weth_usdc 회귀(가격·빈틈 $ 표기가 구 산식과 일치) + cbeth_weth 신규 표기
(비율·ETH 마크가 병기, 빈틈 $는 mark_px 환산, '비율 출렁임', ratio는 정규
소스라 불안정 주석 없음)를 스텁으로 검증한다.
"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # cp949 콘솔에서 —·한글 출력 보호
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from defi_agent import constants as C
from defi_agent.core.analytics import LpEdge
from defi_agent.core.rebalancer import Rebalancer
from defi_agent.lp.aerodrome import PoolState, Position
from defi_agent.tg.bot import TgInterface


class FakeSettings:
    dry_run = True
    hold_mode = False
    lp_range_pct = 35.0
    lp_max_usdc = 500.0
    rerange_trigger = 0.90
    rerange_cooldown_h = 12.0
    hedge_drift_pct = 5.0
    hl_max_leverage = 1.67
    tg_bot_token = None
    tg_chat_id = None
    status_notify_min = 0
    lp_pair_since = 0

    def __init__(self, pair):
        self.lp_pair = pair


class FakeStore:
    async def get_kv(self, key):
        return None

    async def snapshot(self, **kw):
        pass

    async def log_event(self, kind, desc):
        pass


class FakeHedgeState:
    def __init__(self, short=0.0, mark=0.0, account=0.0, upnl=0.0, funding=5.0):
        self.short_size = short
        self.mark_px = mark
        self.account_value = account
        self.unrealized_pnl = upnl
        self.funding_apr_recent = funding


class FakeHedge:
    def __init__(self, st):
        self._st = st

    def state(self):
        return self._st

    def set_target_short(self, t):
        pass

    def close_all(self):
        pass


class FakeLP:
    def __init__(self, pair_key, price, pos=None, balances=(0.0, 0.0)):
        self.pair = C.LP_PAIRS[pair_key]
        self._price = price
        self._pos = pos
        self._bal = balances

    def pool_state(self):
        return PoolState("0xpool", 0, 0, self._price)

    def find_position(self):
        return self._pos

    def wallet_balances(self):
        return self._bal


fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def make_edge(vol_src, px_chg=0.011, il_usd=-0.30):
    return LpEdge(window_h=24.0, samples=100, fee_apr=0.30, vol=0.50, gamma_apr=0.10,
                  net_apr=0.20, m=3.7, pool_yield=0.08, breakeven_vol=0.80,
                  vol_err=0.07, vol_src=vol_src, fee_usd=0.50, il_usd=il_usd,
                  px_chg=px_chg)


def make_tg(pair, price, pos, hstate):
    s = FakeSettings(pair)
    rb = Rebalancer(s, FakeLP(pair, price, pos, (0.001, 0.0)), FakeHedge(hstate), FakeStore())
    r = asyncio.run(rb.run_cycle())
    return TgInterface(s, rb, FakeStore()), r


pos = Position(1, -100, 100, 5, amount0=0.04564, amount1=108.31,
               owed0=0.00002, owed1=0.11, range_ratio=0.3)

# ── 1) weth_usdc 회귀: 표기가 구 형식·구 수치와 일치 ─────────────────
price = 4243.0
hs = FakeHedgeState(short=0.0504, mark=4247.0, account=61.39, upnl=1.2)
tg, r = make_tg("weth_usdc", price, pos, hs)

check("usd: run_cycle이 eth_usd=풀가격", r.eth_usd == price, f"{r.eth_usd}")
check("usd: 가격 표기 구 형식 유지", tg._px_label(r) == "ETH $4,243.00", tg._px_label(r))

txt = tg._status_text(r)
gap = abs(r.lp_delta - r.hedge_size)
check("usd: 헤지 빈틈 $ = gap×풀가격", f"≈ ${gap * price:,.0f} " in txt)
check("usd: LP 줄에 페어 표기", "LP WETH/USDC $" in txt)
check("usd: 액션 꼬리도 구 형식", "· ETH $4,243.00_" in tg._action_text("x", r))

el = "\n".join(tg._edge_lines(make_edge("hl-30d"), True))
check("usd: 'ETH 출렁임' 라벨", "ETH 출렁임 50%" in el)
check("usd: hl-30d는 불안정 주석 없음", "불안정" not in el)
check("usd: pool 폴백은 불안정 주석", "불안정" in "\n".join(tg._edge_lines(make_edge("pool"), True)))
check("usd: 커버리지 'ETH +1.1%'", "_ETH +1.1% —" in "\n".join(tg._coverage_lines(make_edge("pool"), True)))
check("usd: IL 미미 분기도 ETH 라벨",
      "_(ETH +1.1%)_" in "\n".join(tg._coverage_lines(make_edge("pool", il_usd=-0.01), True)))

# ── 2) cbeth_weth: 비율·마크가 병기, $ 환산은 mark_px ────────────────
ratio = 1.1353
cpos = Position(1, 1250, 1290, 1, amount0=0.0207, amount1=0.0236,
                owed0=0.0, owed1=0.0, range_ratio=0.3)
tg2, r2 = make_tg("cbeth_weth", ratio, cpos, hs)

check("cbeth: run_cycle이 eth_usd=mark_px", r2.eth_usd == hs.mark_px, f"{r2.eth_usd}")
check("cbeth: 가격 표기 = 비율 + ETH 마크가",
      tg2._px_label(r2) == "cbETH/WETH 1.1353 · ETH $4,247.00", tg2._px_label(r2))

txt2 = tg2._status_text(r2)
gap2 = abs(r2.lp_delta - r2.hedge_size)
check("cbeth: 헤지 빈틈 $ = gap×mark_px", f"≈ ${gap2 * hs.mark_px:,.0f} " in txt2,
      f"gap={gap2:.4f}")
check("cbeth: 빈틈 $에 비율을 안 씀", f"≈ ${gap2 * ratio:,.0f} " not in txt2)
check("cbeth: LP 줄에 페어 표기", "LP cbETH/WETH $" in txt2)
check("cbeth: 액션 꼬리에 비율+마크가", "cbETH/WETH 1.1353 · ETH $4,247.00_" in tg2._action_text("x", r2))

el2 = "\n".join(tg2._edge_lines(make_edge("ratio"), False))
check("cbeth: '비율 출렁임' 라벨", "비율 출렁임 50%" in el2)
check("cbeth: ratio는 정규 소스 — 불안정 주석 없음", "불안정" not in el2)
check("cbeth: 커버리지 '비율 +1.1%'", "_비율 +1.1% —" in "\n".join(tg2._coverage_lines(make_edge("ratio"), False)))
check("cbeth: IL 미미 분기도 비율 라벨",
      "_(비율 +1.1%)_" in "\n".join(tg2._coverage_lines(make_edge("ratio", il_usd=-0.01), False)))

# ── 3) _status_text가 rb 플래그를 실제로 넘기는지 (호출 경로 결합) ────
check("cbeth: _status_text(edge=…)가 비율 라벨 경로를 탐",
      "비율 출렁임" in tg2._status_text(r2, edge=make_edge("ratio")))
check("usd: _status_text(edge=…)가 ETH 라벨 경로를 탐",
      "ETH 출렁임" in tg._status_text(r, edge=make_edge("hl-30d")))

print()
if fails:
    print(f"실패 {len(fails)}건: {fails}")
    sys.exit(1)
print("전체 통과")
