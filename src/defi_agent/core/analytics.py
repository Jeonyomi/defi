"""LP 레그 경제성 측정 — 수수료 vs 감마손실(LVR).

핵심 결과 (수치검증 완료, docs/backtest.md §5.2 정정 참조):

레인지 안에 있는 동안 수수료와 감마손실은 **둘 다 정확히 m배**로 스케일한다.
  fee_apr   = m × pool_yield          (pool_yield = 풀레인지 수수료수익률)
  gamma_apr = m × sigma^2 / 8         (LVR, 연속헤지 기준)

따라서 순마진의 **부호는 m과 무관**하다:
  net = m × (pool_yield - sigma^2/8)

breakeven 변동성에서 m이 소거된다:
  sigma* = sqrt(8 × pool_yield)

즉 레인지를 좁혀 수수료를 늘려도 감마손실이 같은 배수로 늘어 부호가 안 바뀐다.
m은 "수익 레버"가 아니라 "배율기"다 — 부호가 음수면 손실만 키운다.
게다가 레인지를 벗어나면 수수료는 0이 되지만 이미 낸 감마손실은 IL로 확정되므로,
이탈 위험까지 감안하면 좁은 레인지는 비대칭적으로 불리하다.

검증 기록 (Monte-Carlo, N=20k, 200 paths):
  ±35% sig=30%: sim -6.74% vs 해석식 -6.74%  (ratio 1.001)
  ±35% sig=50%: sim -18.59% vs -18.71%       (ratio 0.993)
  ±10%는 레인지 이탈로 손실이 포화 -> 해석식이 과대추정 (ratio 0.71~0.89).
  즉 m·sigma^2/8은 레인지 안에서 정확하고, 이탈 시 보수적(과대)이다.

변동성 소스 (중요 — 실측으로 확정):
  LVR/감마손실은 **외부 시장가격** 기준으로 정의되므로 풀 slot0가 아니라
  HL mark를 쓴다. 그리고 sigma는 에이전트 스냅샷이 아니라 **HL 캔들**에서 잰다.

  한때 "풀 slot0를 10분 샘플링하면 무차익 밴드 진동이 sigma를 부풀린다"고
  보고 stride를 늘리면 vol이 47.9%->36.2%로 내려가는 걸 근거로 삼았다.
  이는 오판이었다. 같은 구간을 HL mark로 재면 오히려 더 높고(10분 55.5% vs
  풀 48.0%), stride 의존성은 HL mark에도 똑같이 나타나며 단조도 아니다.
  즉 그건 마이크로구조 편향이 아니라 표본이 적어서 생긴 추정 노이즈였다
  (stride=8이면 n=10~17, 상대오차 ±20~30%).
  풀 slot0는 오히려 차익거래가 있어야만 갱신되는 lagged 가격이라 sigma를
  **과소**추정한다. 어느 쪽이든 slot0는 sigma 소스로 부적합하다.

  견고한 실측 (HL 캔들): 24h 47.7% / 7d 49.2% / 30d 56.9% / 90d 50.4%
  (n=96~720, 통계오차 ±3~7%). ETH sigma는 ~50% 수준이다.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

SECONDS_PER_YEAR = 365 * 24 * 3600

# 수수료 실측에 요구하는 최소 관측 창. sigma는 HL 캔들에서 별도로 견고하게
# 재므로 창 길이는 수수료 쪽에만 걸린다.
MIN_WINDOW_H = 6.0


@dataclass
class LpEdge:
    """LP 레그 경제성 스냅샷. 모든 APR은 LP 노셔널 대비 소수(0.09 = 9%)."""

    window_h: float
    samples: int
    fee_apr: float          # 실측 수수료 APR
    vol: float              # 실현 변동성 (연율)
    gamma_apr: float        # 추정 감마손실 APR (양수 = 손실 크기)
    net_apr: float          # fee - gamma
    m: float                # 집중도
    pool_yield: float       # 풀레인지 환산 수수료수익률 = fee_apr / m
    breakeven_vol: float    # sqrt(8 * pool_yield) — m과 무관
    vol_err: float          # 변동성 상대표준오차
    vol_src: str            # "hl-30d" (권장) | "pool" (폴백, slot0는 sigma 과소추정)

    @property
    def verdict(self) -> str:
        """부호 판정. 근거가 부족하면 단정하지 않는다."""
        if self.samples < 30 or self.window_h < MIN_WINDOW_H:
            return "unknown"
        if self.breakeven_vol <= 0 or self.vol <= 0:
            return "unknown"
        lo = self.vol * (1 - self.vol_err)
        hi = self.vol * (1 + self.vol_err)
        if hi < self.breakeven_vol:
            return "positive"
        if lo > self.breakeven_vol:
            return "negative"
        return "marginal"


def realized_vol(rows: list[tuple[int, float]], stride: int = 1) -> tuple[float, int]:
    """(ts, price) 시계열에서 연율 변동성. 반환 (vol, 수익률 표본수).

    stride > 1이면 표본을 솎아 더 긴 간격으로 잰다. 마이크로구조 노이즈는
    간격이 길수록 희석되므로, stride를 바꿔가며 재면 노이즈 편향이 드러난다.
    """
    pts = [(t, p) for t, p in rows if p and p > 0][::max(1, stride)]
    rets, dts = [], []
    for i in range(1, len(pts)):
        dt = pts[i][0] - pts[i - 1][0]
        if dt <= 0:
            continue
        rets.append(math.log(pts[i][1] / pts[i - 1][1]))
        dts.append(dt)
    if len(rets) < 2:
        return 0.0, len(rets)
    step = statistics.mean(dts)
    return statistics.pstdev(rets) * math.sqrt(SECONDS_PER_YEAR / step), len(rets)


def gamma_apr(vol: float, m: float) -> float:
    """연속헤지 CL 포지션의 감마손실(LVR) APR = m * sigma^2 / 8. 양수 = 손실."""
    return m * vol * vol / 8.0


def breakeven_vol(pool_yield: float) -> float:
    """LP 레그 손익분기 변동성 = sqrt(8 * pool_yield). m이 소거되어 레인지와 무관."""
    return math.sqrt(8.0 * pool_yield) if pool_yield > 0 else 0.0


def compute_edge(rows: list[tuple], m: float,
                 vol_ref: tuple[float, int] | None = None) -> LpEdge | None:
    """스냅샷 행 -> LpEdge.

    rows: (ts, price, owed_weth, owed_usdc, lp_weth, lp_usdc, mark_px) 오름차순.
    mark_px는 구버전 스냅샷에 없으므로(NULL/0) 부족하면 price로 폴백한다.
    수수료가 실측된(owed>0) 구간만 유효하다. 재배치/collect가 끼면 owed가
    0으로 리셋되므로, 마지막 단조증가 구간만 잘라 쓴다.
    """
    pts = [r for r in rows if r[1] and r[1] > 0]
    if len(pts) < 3:
        return None

    def fee_usd(r) -> float:
        return r[2] * r[1] + r[3]

    # owed가 감소하면 collect/재배치 -> 그 이후 구간만 사용
    start = 0
    for i in range(1, len(pts)):
        if fee_usd(pts[i]) < fee_usd(pts[i - 1]) - 1e-12:
            start = i
    seg = pts[start:]
    if len(seg) < 3:
        return None

    h = (seg[-1][0] - seg[0][0]) / 3600.0
    if h <= 0:
        return None

    lp_usd = seg[-1][4] * seg[-1][1] + seg[-1][5]
    if lp_usd <= 0:
        return None

    d_fee = fee_usd(seg[-1]) - fee_usd(seg[0])
    fee = d_fee / lp_usd * (8760.0 / h)

    # sigma는 HL 캔들(vol_ref)이 정답. 못 받으면 풀 slot0로 폴백하되
    # 표시에 드러내 판정을 신뢰하지 않게 한다.
    if vol_ref and vol_ref[0] > 0 and vol_ref[1] >= 30:
        vol, n, vol_src = vol_ref[0], vol_ref[1], "hl-30d"
    else:
        vol, n = realized_vol([(r[0], r[1]) for r in seg])
        vol_src = "pool"

    g = gamma_apr(vol, m)
    py = fee / m if m > 0 else 0.0
    err = 1.0 / math.sqrt(2 * n) if n >= 2 else 1.0

    return LpEdge(
        window_h=h, samples=n, fee_apr=fee, vol=vol, gamma_apr=g,
        net_apr=fee - g, m=m, pool_yield=py,
        breakeven_vol=breakeven_vol(py), vol_err=err, vol_src=vol_src,
    )
