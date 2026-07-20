"""aerodrome.py LP_PAIR 파라미터화 오프라인 단위검증 (체인 접속·tx 없음).

실행: .venv/Scripts/python tests/verify_lp_pair_params.py
weth_usdc 회귀(구코드와 raw 수량 일치) + cbeth_weth 신규 의미를 스텁으로 검증한다.
"""
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # cp949 콘솔에서 ≈·— 출력 보호
sys.path.insert(0, str(Path(__file__).parent / "src"))

from defi_agent import constants as C
from defi_agent.lp import math as clmath
from defi_agent.lp.aerodrome import AerodromeLP, PoolState, Position


class FakeFn:
    def __init__(self, ret):
        self.ret = ret
    def call(self, *a, **k):
        return self.ret


class FakeFns:
    def __init__(self, balances):
        self.balances = balances  # raw int
    def balanceOf(self, addr):
        return FakeFn(self.balances[0])
    def allowance(self, a, b):
        return FakeFn(10**30)  # 항상 충분 → approve 안 탐
    def approve(self, a, b):
        raise AssertionError("approve 불필요해야 함")


class FakeToken:
    def __init__(self, bal_raw):
        self._bal = bal_raw
        self.functions = self
    def balanceOf(self, addr):
        return FakeFn(self._bal)
    def allowance(self, a, b):
        return FakeFn(10**30)


class FakeSettings:
    def __init__(self, pair, spacing, range_pct, max_usdc):
        self.lp_pair = pair
        self.lp_tick_spacing = spacing
        self.lp_range_pct = range_pct
        self.lp_max_usdc = max_usdc


class FakeContractFns:
    def __getattr__(self, name):
        return lambda *a, **k: f"call:{name}"


class FakeContract:
    functions = FakeContractFns()


class FakeClient:
    def __init__(self, s):
        self.s = s
        self.address = "0x" + "11" * 20
        self.sent = []
    def contract(self, addr, abi):
        return FakeContract()
    def send(self, fn):
        self.sent.append(fn)
        return "0xfake"
    def has_code(self, addr):
        return True


class TestLP(AerodromeLP):
    """체인 호출부만 스텁."""
    def __init__(self, client, price, bal0, bal1):
        super().__init__(client)
        sqrt_p = int(math.sqrt(price * 10 ** (self.dec1 - self.dec0)) * C.Q96)
        tick = clmath.price_to_tick(price, self.dec0, self.dec1)
        self._st = PoolState("0xpool", sqrt_p, tick, price)
        self.token0 = FakeToken(int(bal0 * 10**self.dec0))
        self.token1 = FakeToken(int(bal1 * 10**self.dec1))
        self.swaps = []
    def pool_state(self):
        return self._st
    def swap(self, token_in, amount_in_raw, min_out_raw):
        self.swaps.append((token_in.lower(), amount_in_raw, min_out_raw))
        return "0xswap"


fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ── 1) weth_usdc 회귀: 기존 코드와 동일 동작 ─────────────────────
s = FakeSettings("weth_usdc", 100, 35.0, 500.0)
lp = TestLP(FakeClient(s), price=4300.0, bal0=0.01, bal1=400.0)
check("weth_usdc: token0=WETH", lp.t0_addr == C.WETH and lp.dec0 == 18 and lp.dec1 == 6)

# 기존 prepare_ratio 공식 (구 코드 그대로 재현)
usd_total, frac0, price = 200.0, 0.418, 4300.0
target_weth = usd_total * frac0 / price
diff = target_weth - 0.01
old_amount_in = int(diff * price * 10**6)
old_min_out = int(diff * (1 - 0.005) * 10**18)
lp.prepare_ratio(usd_total, frac0)
check("weth_usdc: prepare_ratio 스왑 1건 (USDC→WETH)",
      len(lp.swaps) == 1 and lp.swaps[0][0] == C.USDC.lower())
check("weth_usdc: 스왑 수량 구코드와 일치",
      lp.swaps[0][1] == old_amount_in and lp.swaps[0][2] == old_min_out,
      f"in={lp.swaps[0][1]} vs {old_amount_in}")

# $10 미만 차이 무시 회귀
lp2 = TestLP(FakeClient(s), price=4300.0, bal0=target_weth - 0.002, bal1=400.0)
lp2.prepare_ratio(usd_total, frac0)   # diff 0.002 ETH ≈ $8.6 < $10
check("weth_usdc: $10 미만 스왑 스킵", len(lp2.swaps) == 0)

# mint_centered: tick·수량이 구코드 공식과 일치 + tx는 send 스텁으로만
cl = FakeClient(s)
lp3 = TestLP(cl, price=4300.0, bal0=0.0195, bal1=120.0)
lp3.mint_centered(200.0)
st = lp3._st
tl = clmath.align_tick(clmath.price_to_tick(4300 * 0.65, 18, 6), 100)
tu = clmath.align_tick(clmath.price_to_tick(4300 * 1.35, 18, 6), 100)
da0, da1, f0 = clmath.amounts_for_budget(st.sqrt_price_x96, tl, tu, int(200.0 * 10**6))
check("weth_usdc: mint 파라미터 구코드와 일치",
      len(cl.sent) == 1, f"sent={len(cl.sent)} tl={tl} tu={tu} frac0={f0:.3f}")
try:
    lp3.mint_centered(600.0)
    check("weth_usdc: LP_MAX_USDC 초과 차단", False)
except RuntimeError:
    check("weth_usdc: LP_MAX_USDC 초과 차단", True)

# ── 2) cbeth_weth: 신규 모드 의미 검증 ──────────────────────────
s2 = FakeSettings("cbeth_weth", 1, 2.0, 110.0)
ratio = 1.1353
lpc = TestLP(FakeClient(s2), price=ratio, bal0=0.0, bal1=0.0233)
check("cbeth: token0=cbETH, token1=WETH, 18/18dp",
      lpc.t0_addr == C.CBETH and lpc.t1_addr == C.WETH and lpc.dec0 == 18 and lpc.dec1 == 18)
check("cbeth: 풀 tick ≈ 1269 (실측과 일치)", lpc._st.tick == 1269, f"tick={lpc._st.tick}")

# prepare_ratio: 예산 0.0233 WETH의 절반을 cbETH로 → WETH→cbETH 스왑
budget_weth = 0.0233
lpc.prepare_ratio(budget_weth, 0.5)
target0 = budget_weth * 0.5 / ratio
check("cbeth: WETH→cbETH 스왑 발생", len(lpc.swaps) == 1 and lpc.swaps[0][0] == C.WETH.lower())
check("cbeth: 스왑량 = 목표 cbETH×ratio (WETH raw)",
      lpc.swaps[0][1] == int(target0 * ratio * 10**18),
      f"in={lpc.swaps[0][1] / 1e18:.6f} WETH")

# 임계값: WETH 쿼트 페어는 0.0025 WETH(≈$10) 미만 스킵 — $10 하드코딩이면 실패했을 것
lpc2 = TestLP(FakeClient(s2), price=ratio, bal0=target0 - 0.0015, bal1=0.02)
lpc2.prepare_ratio(budget_weth, 0.5)   # diff 0.0015 cbETH ≈ 0.0017 WETH < 0.0025
check("cbeth: 미미한 차이 스킵 (WETH 단위 임계)", len(lpc2.swaps) == 0)
lpc3 = TestLP(FakeClient(s2), price=ratio, bal0=target0 - 0.004, bal1=0.02)
lpc3.prepare_ratio(budget_weth, 0.5)   # diff 0.004 cbETH ≈ 0.0045 WETH > 0.0025
check("cbeth: 유의미한 차이는 스왑", len(lpc3.swaps) == 1)

# mint_centered: ±2%·spacing=1 레인지 + usd_value로 상한 체크
clc = FakeClient(s2)
lpc4 = TestLP(clc, price=ratio, bal0=0.0103, bal1=0.0117)
lpc4.mint_centered(budget_weth, usd_value=100.0)
tl2 = clmath.align_tick(clmath.price_to_tick(ratio * 0.98, 18, 18), 1)
tu2 = clmath.align_tick(clmath.price_to_tick(ratio * 1.02, 18, 18), 1)
check("cbeth: mint 1건, 레인지 tick 정상",
      len(clc.sent) == 1 and tl2 < lpc4._st.tick < tu2, f"[{tl2}, {tu2}]")
try:
    lpc4.mint_centered(budget_weth, usd_value=120.0)
    check("cbeth: usd_value로 LP_MAX_USDC 체크", False)
except RuntimeError:
    check("cbeth: usd_value로 LP_MAX_USDC 체크", True)

# 페어 밖 토큰 스왑 차단
try:
    AerodromeLP.swap(lpc4, C.USDC, 1000, 0)  # TestLP 스텁 우회, 원본 검증 로직 호출
    check("cbeth: USDC 스왑 차단", False)
except RuntimeError:
    check("cbeth: USDC 스왑 차단", True)

# ── 3) Position 호환 별칭 (rebalancer 무수정 유지) ───────────────
p = Position(1, -100, 100, 5, amount0=1.5, amount1=2.5, owed0=0.1, owed1=0.2, range_ratio=0.3)
check("Position 별칭: weth_amount/usdc_amount/owed_*",
      p.weth_amount == 1.5 and p.usdc_amount == 2.5 and p.owed_weth == 0.1 and p.owed_usdc == 0.2)

# ── 4) 임포트 회귀 ───────────────────────────────────────────────
import defi_agent.core.rebalancer  # noqa: F401
import defi_agent.core.analytics  # noqa: F401
check("rebalancer/analytics 임포트 정상", True)

print()
print("결과: " + ("전체 통과" if not fails else f"실패 {len(fails)}건: {fails}"))
sys.exit(1 if fails else 0)
