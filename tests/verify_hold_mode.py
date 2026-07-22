"""HOLD_MODE(cbETH 단순보유 + 숏 헤지) 오프라인 단위검증 (체인·HL 접속 없음, tx 없음).

실행: .venv/Scripts/python tests/verify_hold_mode.py
2026-07-22 전환 근거: cbETH/WETH LP 수수료 APR 실측 ~0 (iter29) → LP 레그 제거.
검증 항목:
  1) hold 모드 + LP 없음 → mint 금지 (자동진입 차단)
  2) 지갑 ETH-eq가 델타로 잡혀 드리프트 시 재헤지 발동
  3) 갭 $16 미만이면 재헤지 보류 (기존 규율 유지)
  4) 회귀: hold 꺼짐 + LP 없음 → 기존 자동진입 그대로
  5) 상태 메시지: 보유 모드 줄 + 레버리지 표기, LP 수지 블록 생략

스텁은 verify_rebalancer_pair.py와 동일 구조 — 그쪽은 모듈 레벨에서 sys.exit해
import할 수 없어 사본을 둔다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from defi_agent import constants as C
from defi_agent.core.rebalancer import CycleReport, Rebalancer
from defi_agent.lp.aerodrome import PoolState
from defi_agent.tg.bot import TgInterface


class FakeSettings:
    dry_run = True
    hold_mode = False
    lp_range_pct = 2.0
    lp_max_usdc = 110.0
    rerange_trigger = 0.90
    rerange_cooldown_h = 12.0
    hedge_drift_pct = 5.0
    hl_max_leverage = 3.0
    tg_bot_token = None
    tg_chat_id = None
    status_notify_min = 0
    lp_pair_since = 0

    def __init__(self, pair, **kw):
        self.lp_pair = pair
        for k, v in kw.items():
            setattr(self, k, v)


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
        self.targets = []
        self.closed = 0

    def state(self):
        return self._st

    def set_target_short(self, t):
        self.targets.append(t)

    def close_all(self):
        self.closed += 1


class FakeLP:
    def __init__(self, pair_key, price, pos=None, balances=(0.0, 0.0)):
        self.pair = C.LP_PAIRS[pair_key]
        self._price = price
        self._pos = pos
        self._bal = balances
        self.mints = []

    def pool_state(self):
        return PoolState("0xpool", 0, 0, self._price)

    def find_position(self):
        return self._pos

    def wallet_balances(self):
        return self._bal

    def mint_centered(self, budget_t1, slippage=0.01, usd_value=None):
        self.mints.append((budget_t1, usd_value))
        return "0xmint"


fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def near(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def cycle(pair, price, pos, balances, hstate, **skw):
    s = FakeSettings(pair, **skw)
    lp = FakeLP(pair, price, pos, balances)
    hedge = FakeHedge(hstate)
    rb = Rebalancer(s, lp, hedge, FakeStore())
    r = asyncio.run(rb.run_cycle())
    return r, lp, hedge


# 실전 근사값: cbETH 0.28 + WETH 0.003 보유, 비율 1.1353, ETH $1,900
RATIO, MARK = 1.1353, 1900.0
BAL = (0.28, 0.003)
WALLET_DELTA = BAL[0] * RATIO + BAL[1]  # ≈ 0.3209 ETH-eq

# ── 1) hold 모드: LP 없음 + 지갑에 돈이 있어도 mint 금지 ──
hs = FakeHedgeState(short=WALLET_DELTA, mark=MARK, account=230.0)
r, lp, hedge = cycle("cbeth_weth", RATIO, None, BAL, hs, hold_mode=True)
check("1a mint 0건 (자동진입 차단)", lp.mints == [])
check("1b 델타=지갑 ETH-eq", near(r.lp_delta, WALLET_DELTA, 1e-6), f"{r.lp_delta:.4f}")
check("1c 숏 일치 시 재헤지 없음", hedge.targets == [])
check("1d equity=지갑+HL", near(r.equity, WALLET_DELTA * MARK + 230.0, 1e-6))

# ── 2) 드리프트 발동: 숏이 지갑 델타보다 10% 작으면 재헤지 ──
hs = FakeHedgeState(short=WALLET_DELTA * 0.90, mark=MARK, account=230.0)
r, lp, hedge = cycle("cbeth_weth", RATIO, None, BAL, hs, hold_mode=True)
check("2a 재헤지 발동 → 목표=지갑 델타",
      hedge.targets and near(hedge.targets[0], WALLET_DELTA, 1e-6), f"{hedge.targets}")

# ── 3) 갭 $16 미만 보류 (드리프트 100%지만 노셔널 ~$13) ──
small_bal = (0.006, 0.0)
hs = FakeHedgeState(short=0.0, mark=MARK, account=230.0)
r, lp, hedge = cycle("cbeth_weth", RATIO, None, small_bal, hs, hold_mode=True)
check("3a 소액 갭 재헤지 보류", hedge.targets == [],
      f"delta=${small_bal[0] * RATIO * MARK:.0f}")

# ── 4) 회귀: hold 꺼짐 + LP 없음 → 기존 자동진입 경로 그대로 ──
hs = FakeHedgeState(short=0.0, mark=MARK, account=230.0)
r, lp, hedge = cycle("cbeth_weth", RATIO, None, (0.0, 0.3), hs,
                     hold_mode=False, lp_max_usdc=600.0)
check("4a 자동진입 유지 (mint 1건)", len(lp.mints) == 1)

# ── 5) 상태 메시지 렌더 ──
class _RB:
    price_is_usd = False
    full_delta = True
    pair_label = "cbETH/WETH"
    paused = False


class _TG(TgInterface):
    def __init__(self, s):
        self.s = s
        self.rb = _RB()


rep = CycleReport(ts=0, price=RATIO, eth_usd=MARK, equity=840.0, lp_value=0.0,
                  hedge_size=0.3206, lp_delta=0.3209, hl_account=230.0,
                  wallet_usd=610.0, eff_lev=2.65, funding_apr=11.0, hedge_upnl=3.0)
txt = _TG(FakeSettings("cbeth_weth", hold_mode=True))._status_text(rep, None, "상태", edge=None)
check("5a 보유 모드 줄", "보유 모드" in txt and "cbETH 단순보유" in txt)
check("5b 레버리지 줄 유지", "레버리지 2.65x" in txt)
check("5c 총자산 줄이 보유 표기", "cbETH 보유 $610.00" in txt)
check("5d LP 진입 대기 문구 없음", "진입 대기" not in txt)
check("5e LP 수지 블록 없음", "LP 수지" not in txt)

txt2 = _TG(FakeSettings("cbeth_weth", hold_mode=False))._status_text(rep, None, "상태", edge=None)
check("5f hold 꺼짐 회귀 (진입 대기 표기)", "진입 대기" in txt2)

print()
if fails:
    print(f"FAILED: {len(fails)}건 — {fails}")
    sys.exit(1)
print("verify_hold_mode: 전체 통과")
