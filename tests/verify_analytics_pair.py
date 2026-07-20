"""analytics.py 페어 일반화 오프라인 단위검증 (체인·HL 접속 없음, tx 없음).

실행: .venv/Scripts/python tests/verify_analytics_pair.py
weth_usdc 회귀(구 산식과 수치 일치) + cbeth_weth 신규 의미(mark_px USD 환산·
비율 sigma·vol_ref 무시·창 시작 강제)를 합성 스냅샷으로 검증한다.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from defi_agent.config import Settings
from defi_agent.core.analytics import compute_edge, realized_vol

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {name}")


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


# ---------- 1. weth_usdc 회귀: 구 산식과 수치 일치 ----------
# 10분 간격 7행, 가격 3800→3812, owed 단조증가, LP 원금 고정.
T0 = 1_784_500_000
rows_usd = []
for i in range(7):
    price = 3800.0 + 2.0 * i
    rows_usd.append((T0 + 600 * i, price,
                     0.0001 * i,          # owed_weth
                     0.3 * i,             # owed_usdc
                     0.045,               # lp_weth
                     108.0,               # lp_usdc
                     price + 1.0))        # mark_px (usd모드에선 미사용)

e = compute_edge(rows_usd, m=3.4, vol_ref=(0.50, 720), price_is_usd=True)
check("usd: 결과 존재", e is not None)

# 구 산식 재계산 (변경 전 코드와 동일한 손계산)
fee_q = lambda r: r[2] * r[1] + r[3]
seg = rows_usd
h = (seg[-1][0] - seg[0][0]) / 3600.0
lp_usd = seg[-1][4] * seg[-1][1] + seg[-1][5]
d_fee = fee_q(seg[-1]) - fee_q(seg[0])
p1 = seg[-1][1]
il = (seg[-1][4] * p1 + seg[-1][5]) - (seg[0][4] * p1 + seg[0][5])
check("usd: fee_usd 구 산식 일치", approx(e.fee_usd, d_fee))
check("usd: fee_apr 구 산식 일치", approx(e.fee_apr, d_fee / lp_usd * (8760.0 / h)))
check("usd: il_usd 구 산식 일치 (원금 고정 -> 0)", approx(e.il_usd, il) and abs(il) < 1e-12)
check("usd: vol_ref 채택 (hl-30d)", e.vol_src == "hl-30d" and e.vol == 0.50)
check("usd: px_chg", approx(e.px_chg, 3812.0 / 3800.0 - 1.0))

# vol_ref 없으면 pool 폴백 (구 동작 유지)
e2 = compute_edge(rows_usd, m=3.4, vol_ref=None, price_is_usd=True)
check("usd: vol_ref 없음 -> pool 폴백", e2.vol_src == "pool")

# ---------- 2. cbeth_weth: mark_px USD 환산 + 비율 sigma ----------
# price = WETH per cbETH 비율 ~1.135, owed0=cbETH, owed1=WETH, mark_px = HL ETH.
MARK_LAST = 3820.0
rows_cb = []
for i in range(7):
    ratio = 1.1350 + 0.0002 * i
    rows_cb.append((T0 + 600 * i, ratio,
                    0.00001 * i,         # owed0 (cbETH)
                    0.00001 * i,         # owed1 (WETH)
                    0.0116,              # amount0 (cbETH)
                    0.0132,              # amount1 (WETH)
                    3800.0 if i < 6 else MARK_LAST))

# vol_ref를 일부러 넘겨도 무시해야 한다 (ETH/USD vol은 비율 감마에 오답)
e = compute_edge(rows_cb, m=57.0, vol_ref=(0.50, 720), price_is_usd=False)
check("cb: 결과 존재", e is not None)

seg = rows_cb
p1 = seg[-1][1]
d_fee_q = fee_q(seg[-1]) - fee_q(seg[0])                     # WETH 단위
lp_q = seg[-1][4] * p1 + seg[-1][5]                          # WETH 단위
il_q = (seg[-1][4] * p1 + seg[-1][5]) - (seg[0][4] * p1 + seg[0][5])
check("cb: fee_usd = ΔfeeWETH × 마지막 mark", approx(e.fee_usd, d_fee_q * MARK_LAST))
check("cb: fee_apr는 환산과 무관 (비율 소거)",
      approx(e.fee_apr, d_fee_q / lp_q * (8760.0 / ((seg[-1][0] - seg[0][0]) / 3600.0))))
check("cb: il_usd = ILWETH × 마지막 mark (원금 고정 -> 0)",
      approx(e.il_usd, il_q * MARK_LAST) and abs(il_q) < 1e-12)
check("cb: vol_src=ratio (vol_ref 무시)", e.vol_src == "ratio")
vol_ratio, _ = realized_vol([(r[0], r[1]) for r in rows_cb])
check("cb: vol = 비율 시계열 실측", approx(e.vol, vol_ratio) and e.vol != 0.50)
check("cb: px_chg = 비율 변화", approx(e.px_chg, (1.1350 + 0.0002 * 6) / 1.1350 - 1.0))

# mark_px가 창 전체에 없으면 USD 환산 불가 -> None
rows_nomark = [(t, p, o0, o1, a0, a1, 0.0) for (t, p, o0, o1, a0, a1, _) in rows_cb]
check("cb: mark 전무 -> None",
      compute_edge(rows_nomark, m=57.0, price_is_usd=False) is None)

# ---------- 3. 창 시작 강제: 모드 전환 지점(price 급변)에서 절단 ----------
# 구 모드 3행(price~3800) 뒤 신 모드 4행(비율~1.135) — 섞이면 전부 오염.
mixed = []
for i in range(3):
    mixed.append((T0 + 600 * i, 3800.0 + i, 0.001, 50.0, 0.045, 108.0, 3800.0))
for i in range(4):
    mixed.append((T0 + 600 * (3 + i), 1.1350 + 0.0002 * i,
                  0.00001 * i, 0.00001 * i, 0.0116, 0.0132, 3810.0))
e = compute_edge(mixed, m=57.0, price_is_usd=False)
check("mix: 결과 존재", e is not None)
check("mix: 창이 전환 이후 4행만 사용 (30분)", approx(e.window_h, 0.5))
check("mix: px_chg가 비율 구간만 반영", abs(e.px_chg) < 0.01)

# ---------- 4. config: LP_PAIR_SINCE 필드 존재 ----------
check("config: Settings.lp_pair_since 필드", "lp_pair_since" in Settings.__dataclass_fields__)

print(f"\n전체 통과: {PASS}건")
