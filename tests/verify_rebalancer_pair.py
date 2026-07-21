"""rebalancer.py 페어 일반화 오프라인 단위검증 (체인·HL 접속 없음, tx 없음).

실행: .venv/Scripts/python tests/verify_rebalancer_pair.py
weth_usdc 회귀(구 산식과 수치 일치) + cbeth_weth 신규 의미(풀델타·mark_px 환산·
need_margin 1.0·mint 예산 WETH 단위)를 스텁으로 검증한다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from defi_agent import constants as C
from defi_agent.core.rebalancer import Rebalancer
from defi_agent.lp.aerodrome import PoolState, Position


class FakeSettings:
    dry_run = True
    lp_range_pct = 35.0
    lp_max_usdc = 500.0
    rerange_trigger = 0.90
    rerange_cooldown_h = 12.0
    hedge_drift_pct = 5.0
    hl_max_leverage = 1.67

    def __init__(self, pair, **kw):
        self.lp_pair = pair
        for k, v in kw.items():
            setattr(self, k, v)


class FakeStore:
    def __init__(self):
        self.snapshots = []
        self.events = []

    async def get_kv(self, key):
        return None

    async def snapshot(self, **kw):
        self.snapshots.append(kw)

    async def log_event(self, kind, desc):
        self.events.append((kind, desc))


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
        self.closes = 0
        self.collects = 0

    def pool_state(self):
        return PoolState("0xpool", 0, 0, self._price)

    def find_position(self):
        return self._pos

    def wallet_balances(self):
        return self._bal

    def mint_centered(self, budget_t1, slippage=0.01, usd_value=None):
        self.mints.append((budget_t1, usd_value))
        return "0xmint"

    def close_position(self, pos, slippage=0.02):
        self.closes += 1

    def collect_fees(self, pos):
        self.collects += 1


def make_pos(a0, a1, o0, o1, ratio=0.3):
    return Position(1, -100, 100, 5, amount0=a0, amount1=a1,
                    owed0=o0, owed1=o1, range_ratio=ratio)


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
    store = FakeStore()
    rb = Rebalancer(s, lp, hedge, store)
    r = asyncio.run(rb.run_cycle())
    return r, lp, hedge, store, rb


# ── 1) weth_usdc 회귀: 현행 라이브와 같은 형태의 수치로 구 산식 일치 ──
price = 4243.0
pos = make_pos(0.04564, 108.31, 0.00002, 0.11)
hs = FakeHedgeState(short=0.0504, mark=4247.0, account=61.39, upnl=1.2)
r, lp, hedge, store, _ = cycle("weth_usdc", price, pos, (0.00209, 0.0), hs)

old_delta = pos.amount0 + pos.owed0
old_value = old_delta * price + pos.amount1 + pos.owed1
old_owed = pos.owed0 * price + pos.owed1
old_wallet = 0.00209 * price + 0.0
check("weth_usdc: lp_delta 구 산식 일치", near(r.lp_delta, old_delta), f"{r.lp_delta}")
check("weth_usdc: lp_value 구 산식 일치", near(r.lp_value, old_value), f"{r.lp_value:.2f}")
check("weth_usdc: owed_usd 구 산식 일치", near(r.owed_usd, old_owed))
check("weth_usdc: wallet_usd 구 산식 일치", near(r.wallet_usd, old_wallet))
check("weth_usdc: equity 합산", near(r.equity, old_value + old_wallet + 61.39))
check("weth_usdc: 스냅샷 컬럼 = token0/1 원시량",
      store.snapshots[0]["lp_weth"] == pos.amount0 and store.snapshots[0]["lp_usdc"] == pos.amount1)
exp_drift = abs(0.0504 - old_delta) / old_delta * 100
check("weth_usdc: drift 산식 일치", near(r.drift_pct, exp_drift), f"{r.drift_pct:.2f}%")
# 드리프트 큰 케이스 → 재헤지 타깃 = lp_delta (구코드 동일)
hs2 = FakeHedgeState(short=0.0300, mark=4247.0, account=61.39)
r2, _, hedge2, _, _ = cycle("weth_usdc", price, pos, (0.0, 0.0), hs2)
check("weth_usdc: 재헤지 타깃 = token0 델타",
      hedge2.targets == [r2.lp_delta] and near(r2.lp_delta, old_delta))

# ── 2) weth_usdc 진입 회귀 ─────────────────────────────────────
hs3 = FakeHedgeState(short=0.0, mark=4247.0, account=200.0)
r3, lp3, hedge3, _, _ = cycle("weth_usdc", price, None, (0.0, 400.0), hs3)
check("weth_usdc 진입: mint 예산 = USDC 그대로 + usd_value 동일",
      lp3.mints == [(400.0, 400.0)])
check("weth_usdc 진입: 폴백 헤지 = 예산×0.42/풀가",
      len(hedge3.targets) == 1 and near(hedge3.targets[0], 400.0 * 0.42 / price))
# 증거금 부족 → 진입 보류 (need_margin = 400×0.5/1.67×1.2 ≈ $143.7)
hs4 = FakeHedgeState(short=0.0, mark=4247.0, account=100.0)
r4, lp4, _, _, _ = cycle("weth_usdc", price, None, (0.0, 400.0), hs4)
check("weth_usdc 진입: need_margin 0.5 계수 (잔고 $100 < $143.7 → 보류)",
      lp4.mints == [] and any("미뤘습니다" in a for a in r4.alerts))

# ── 3) cbeth_weth: 풀델타·mark_px 환산 ─────────────────────────
ratio, mark = 1.1353, 4250.0
cpos = make_pos(0.0103, 0.0117, 0.0001, 0.0002)
chs = FakeHedgeState(short=0.0535, mark=mark, account=61.0)
rc, clp, chedge, cstore, _ = cycle("cbeth_weth", ratio, cpos, (0.0, 0.001), chs,
                                   lp_max_usdc=110.0, lp_range_pct=2.0)
exp_delta = (0.0103 + 0.0001) * ratio + 0.0117 + 0.0002
exp_value = exp_delta * mark
exp_owed = (0.0001 * ratio + 0.0002) * mark
check("cbeth: lp_delta = cbETH×비율 + WETH (owed 포함)", near(rc.lp_delta, exp_delta),
      f"{rc.lp_delta:.6f}")
check("cbeth: lp_value = 델타×mark_px", near(rc.lp_value, exp_value), f"${rc.lp_value:.2f}")
check("cbeth: owed_usd = mark_px 환산", near(rc.owed_usd, exp_owed))
check("cbeth: wallet_usd = WETH×mark_px", near(rc.wallet_usd, 0.001 * mark))
check("cbeth: 스냅샷 lp_weth 컬럼에 cbETH(token0) 저장",
      cstore.snapshots[0]["lp_weth"] == 0.0103 and cstore.snapshots[0]["lp_usdc"] == 0.0117)
exp_cdrift = abs(0.0535 - exp_delta) / exp_delta * 100
check("cbeth: drift = 숏 vs 풀델타", near(rc.drift_pct, exp_cdrift), f"{rc.drift_pct:.2f}%")

# 드리프트 초과 → 재헤지 타깃 = 풀델타
chs2 = FakeHedgeState(short=0.0300, mark=mark, account=61.0)
rc2, _, chedge2, _, _ = cycle("cbeth_weth", ratio, cpos, (0.0, 0.0), chs2,
                              lp_max_usdc=110.0, lp_range_pct=2.0)
check("cbeth: 재헤지 타깃 = 풀델타", chedge2.targets == [rc2.lp_delta]
      and near(rc2.lp_delta, exp_delta))

# ── 4) cbeth_weth 진입: 예산 WETH 단위·need_margin 1.0·폴백 1.0 ──
# 지갑 0.026 WETH ≈ $110.5 → deployable=min(110.5, 110)=110
chs3 = FakeHedgeState(short=0.0, mark=mark, account=100.0)
rc3, clp3, chedge3, _, _ = cycle("cbeth_weth", ratio, None, (0.0, 0.026), chs3,
                                 lp_max_usdc=110.0, lp_range_pct=2.0)
check("cbeth 진입: mint 예산 = $110/mark (WETH 단위) + usd_value=$110",
      len(clp3.mints) == 1 and near(clp3.mints[0][0], 110.0 / mark)
      and near(clp3.mints[0][1], 110.0), f"{clp3.mints}")
check("cbeth 진입: 폴백 헤지 = 예산 전액/eth_usd (×1.0)",
      len(chedge3.targets) == 1 and near(chedge3.targets[0], 110.0 / mark))
# need_margin = 110×1.0/1.67×1.2 ≈ $79.0 → 잔고 $70이면 보류 (0.5 계수였다면 $39.5로 통과했을 것)
chs4 = FakeHedgeState(short=0.0, mark=mark, account=70.0)
rc4, clp4, _, _, _ = cycle("cbeth_weth", ratio, None, (0.0, 0.026), chs4,
                           lp_max_usdc=110.0, lp_range_pct=2.0)
check("cbeth 진입: need_margin 1.0 계수 (잔고 $70 < $79 → 보류)",
      clp4.mints == [] and any("미뤘습니다" in a for a in rc4.alerts))
# USDC는 wallet_balances에 없음 → 페어 토큰 잔고만으로 deployable 산정 (대기 USDC 비침범)
chs5 = FakeHedgeState(short=0.0, mark=mark, account=100.0)
rc5, clp5, _, _, _ = cycle("cbeth_weth", ratio, None, (0.0, 0.02), chs5,
                           lp_max_usdc=110.0, lp_range_pct=2.0)
check("cbeth 진입: 페어 잔고 $85 < $100 → 진입 안 함 (USDC 미포함)",
      clp5.mints == [] and any("놀고" in a for a in rc5.alerts))

# ── 5) cbeth_weth: mark_px=0 가드 (환산 불가 → 관측만) ──────────
chs6 = FakeHedgeState(short=0.0, mark=0.0, account=100.0)
rc6, clp6, chedge6, cstore6, _ = cycle("cbeth_weth", ratio, None, (0.0, 0.026), chs6,
                                       lp_max_usdc=110.0, lp_range_pct=2.0)
check("cbeth: mark_px=0 → 액션 0건 + 경보 + 스냅샷은 기록",
      clp6.mints == [] and chedge6.targets == [] and len(cstore6.snapshots) == 1
      and any("환산" in a for a in rc6.alerts))

# ── 6) _do_rerange 페어 인지 ────────────────────────────────────
s6 = FakeSettings("cbeth_weth", lp_max_usdc=110.0, lp_range_pct=2.0)
new_pos = make_pos(0.0110, 0.0120, 0.0, 0.0)
lp6 = FakeLP("cbeth_weth", ratio, new_pos)
rb6 = Rebalancer(s6, lp6, FakeHedge(FakeHedgeState(mark=mark)), FakeStore())
rb6._do_rerange(make_pos(0.01, 0.01, 0.0, 0.0), 120.0, mark)
check("cbeth 재배치: close 1건 + mint 예산 = min($120,$110)/mark WETH",
      lp6.closes == 1 and len(lp6.mints) == 1
      and near(lp6.mints[0][0], 110.0 / mark) and near(lp6.mints[0][1], 110.0))
check("cbeth 재배치: 재헤지 타깃 = 새 포지션 풀델타",
      near(rb6.hedge.targets[0], 0.0110 * ratio + 0.0120))

s7 = FakeSettings("weth_usdc")
lp7 = FakeLP("weth_usdc", price, make_pos(0.02, 100.0, 0.0, 0.0))
rb7 = Rebalancer(s7, lp7, FakeHedge(FakeHedgeState(mark=4247.0)), FakeStore())
rb7._do_rerange(make_pos(0.02, 100.0, 0.0, 0.0), 190.0, 4247.0)
check("weth_usdc 재배치 회귀: 예산 USD 그대로 + 타깃 token0 델타",
      lp7.mints == [(190.0, 190.0)] and near(rb7.hedge.targets[0], 0.02))


# ── 7) 재배치 델타 규율: 실패·조회지연 경로에서도 헤지 동기화 ────
class FailMintLP(FakeLP):
    def mint_centered(self, budget_t1, slippage=0.01, usd_value=None):
        raise RuntimeError("PSC")


# mint 실패 → 지갑 실측 델타로 재헤지 + 예외는 그대로 전파 (_act가 기록하도록)
lp8 = FailMintLP("cbeth_weth", ratio, None, balances=(0.05, 0.06))
rb8 = Rebalancer(FakeSettings("cbeth_weth", lp_max_usdc=110.0, lp_range_pct=2.0),
                 lp8, FakeHedge(FakeHedgeState(mark=mark)), FakeStore())
raised8 = False
try:
    rb8._do_rerange(make_pos(0.01, 0.01, 0.0, 0.0), 120.0, mark)
except RuntimeError:
    raised8 = True
check("재배치 mint 실패(cbeth): 지갑 델타(0.05×비율+0.06) 재헤지 + 예외 전파",
      raised8 and len(rb8.hedge.targets) == 1
      and near(rb8.hedge.targets[0], 0.05 * ratio + 0.06))

lp9 = FailMintLP("weth_usdc", price, None, balances=(0.021, 100.0))
rb9 = Rebalancer(FakeSettings("weth_usdc"), lp9,
                 FakeHedge(FakeHedgeState(mark=4247.0)), FakeStore())
raised9 = False
try:
    rb9._do_rerange(make_pos(0.02, 100.0, 0.0, 0.0), 190.0, 4247.0)
except RuntimeError:
    raised9 = True
check("재배치 mint 실패(usd 페어): 지갑 WETH만 재헤지 (USDC 미포함)",
      raised9 and rb9.hedge.targets == [0.021])

# mint 성공 + 포지션 조회 지연(None) → 진입 경로와 같은 추정치로 즉시 동기화
lp10 = FakeLP("cbeth_weth", ratio, None)
rb10 = Rebalancer(FakeSettings("cbeth_weth", lp_max_usdc=110.0, lp_range_pct=2.0),
                  lp10, FakeHedge(FakeHedgeState(mark=mark)), FakeStore())
rb10._do_rerange(make_pos(0.01, 0.01, 0.0, 0.0), 120.0, mark)
check("재배치 조회 지연(cbeth): 추정치 min($120,$110)/mark×1.0으로 즉시 동기화",
      len(rb10.hedge.targets) == 1 and near(rb10.hedge.targets[0], 110.0 / mark))

lp11 = FakeLP("weth_usdc", price, None)
rb11 = Rebalancer(FakeSettings("weth_usdc"), lp11,
                  FakeHedge(FakeHedgeState(mark=4247.0)), FakeStore())
rb11._do_rerange(make_pos(0.02, 100.0, 0.0, 0.0), 190.0, 4247.0)
check("재배치 조회 지연(usd 페어): 추정치 $190×0.42/eth_usd로 즉시 동기화",
      len(rb11.hedge.targets) == 1 and near(rb11.hedge.targets[0], 190.0 * 0.42 / 4247.0))

# --- 2단계 입금 알림 (2.6x 예고 / 2.8x 필수 / 3.0x 차단 상태) ---
# 실측 스냅샷(2026-07-21 저녁) 기반: short 0.32 ETH, mark $1,941 → 노셔널 $621.1
# 경보 블록은 포지션 보유 경로 끝에 있으므로 LP 보유 + 헤지 일치(드리프트≈0) 상태로 통과시킨다.
def lev_alerts(account):
    # 풀델타 = 0.15×비율 + 0.15 ≈ 0.3203 → short 0.32와 드리프트 0.1% (재헤지 미발동)
    pos = make_pos(0.15, 0.15, 0.0, 0.0, ratio=0.3)
    r, *_ = cycle("cbeth_weth", ratio, pos, (0.0, 0.0),
                  FakeHedgeState(short=0.32, mark=1941.0, account=account))
    return [a for a in r.alerts if "[1차" in a or "[2차" in a]

a0 = lev_alerts(260.0)   # 2.39x — 무경보
check("입금알림: 2.39x → 알림 없음", a0 == [])

a1 = lev_alerts(223.2)   # 2.78x — 1차 예고, 2.4x 복귀에 $40
check("입금알림: 2.78x → 1차 예고 + $40 계산",
      len(a1) == 1 and "[1차" in a1[0] and "$40" in a1[0])

a2 = lev_alerts(214.0)   # 2.90x — 2차 필수, $50 + 차단가 $1,957
check("입금알림: 2.90x → 2차 필수 + $50 + 차단가 $1,957",
      len(a2) == 1 and "[2차" in a2[0] and "$50" in a2[0] and "$1,957" in a2[0])

a3 = lev_alerts(200.0)   # 3.11x — 이미 차단 상태 문구
check("입금알림: 3.11x → 차단 상태 문구",
      len(a3) == 1 and "[2차" in a3[0] and "차단된 상태" in a3[0])

print()
print("결과: " + ("전체 통과" if not fails else f"실패 {len(fails)}건: {fails}"))
sys.exit(1 if fails else 0)
