"""Uniswap v3 / Slipstream CL 수학. 모니터링·헤지 계산용.

컨벤션: WETH/USDC 풀에서 token0=WETH, token1=USDC (주소 정렬상 WETH < USDC 아님 —
실제 정렬은 런타임에 pool.token0()으로 확인하고 aerodrome.py가 방향을 맞춘다).
여기 함수들은 token0/token1 기준의 순수 수학만 다룬다.
"""
from __future__ import annotations

import math

from ..constants import Q96


def tick_to_sqrt_price_x96(tick: int) -> int:
    return int(math.sqrt(1.0001**tick) * Q96)


def sqrt_price_x96_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """token1/token0 가격 (사람 단위)."""
    raw = (sqrt_price_x96 / Q96) ** 2
    return raw * 10 ** (dec0 - dec1)


def price_to_tick(price_t1_per_t0: float, dec0: int, dec1: int) -> int:
    raw = price_t1_per_t0 * 10 ** (dec1 - dec0)
    return int(math.floor(math.log(raw, 1.0001)))


def align_tick(tick: int, spacing: int) -> int:
    return (tick // spacing) * spacing


def position_amounts(
    liquidity: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int
) -> tuple[int, int]:
    """포지션이 현재 보유한 (amount0, amount1) raw 단위."""
    sa = tick_to_sqrt_price_x96(tick_lower)
    sb = tick_to_sqrt_price_x96(tick_upper)
    sp = min(max(sqrt_price_x96, sa), sb)
    amount0 = liquidity * Q96 * (sb - sp) // (sp * sb) if sp < sb else 0
    amount1 = liquidity * (sp - sa) // Q96 if sp > sa else 0
    return int(amount0), int(amount1)


def value_fraction_token0(price: float, lo_price: float, hi_price: float) -> float:
    """레인지 [lo, hi]의 CL 포지션에서 token0(리스키)이 차지하는 가치 비율.

    대칭 %레인지라도 50%가 아니다 — 예: ±35% 레인지는 token0 41.8%.
    v0 = s(b-s)/b, v1 = s-a  (s=√P, a=√lo, b=√hi)
    """
    s, a, b = math.sqrt(price), math.sqrt(lo_price), math.sqrt(hi_price)
    s = min(max(s, a), b)
    v0 = s * (b - s) / b
    v1 = s - a
    total = v0 + v1
    return v0 / total if total > 0 else 0.5


def lp_delta_token0(
    liquidity: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int
) -> int:
    """LP 포지션의 델타 = 현재 보유 중인 token0(리스키 자산) 수량 raw.

    CL 포지션 가치의 가격 미분 dV/dP = amount0 이므로,
    헤지 목표 숏 수량 = 포지션 내 token0 수량 (+ 미수령 수수료 token0).
    """
    a0, _ = position_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper)
    return a0


def amounts_for_budget(
    sqrt_price_x96: int, tick_lower: int, tick_upper: int, budget_token1_raw: int
) -> tuple[int, int, float]:
    """정렬된 tick 레인지에 budget(token1 raw 가치)을 넣을 때 필요한
    (amount0_raw, amount1_raw, token0 가치비율). 근사 아닌 정확한 CL 수학 —
    mint의 amountMin 슬리피지 체크(PSC)를 통과하려면 이 값으로 desired를 잡아야 한다.
    """
    sa = tick_to_sqrt_price_x96(tick_lower)
    sb = tick_to_sqrt_price_x96(tick_upper)
    sp = min(max(sqrt_price_x96, sa), sb)
    v0_per_l = (sb - sp) * sp / (sb * Q96)   # 유동성 1단위당 token0 가치 (token1 raw)
    v1_per_l = (sp - sa) / Q96
    total = v0_per_l + v1_per_l
    if total <= 0:
        return 0, 0, 0.5
    liq = budget_token1_raw / total
    a0 = liq * Q96 * (sb - sp) / (sp * sb) if sp < sb else 0
    a1 = liq * (sp - sa) / Q96 if sp > sa else 0
    return int(a0), int(a1), v0_per_l / total


def range_position_ratio(tick: int, tick_lower: int, tick_upper: int) -> float:
    """현재 틱이 레인지 중앙(0)에서 경계(1)까지 어느 지점인지. >=1 이면 이탈."""
    mid = (tick_lower + tick_upper) / 2
    half = (tick_upper - tick_lower) / 2
    if half <= 0:
        return 1.0
    return abs(tick - mid) / half


def concentration_multiplier(lo_price: float, hi_price: float) -> float:
    """레인지 [lo, hi]의 집중도 m — 같은 자본으로 풀레인지 대비 몇 배 유동성인가.

    m = 1 / (1 - (lo/hi)^(1/4)).  IL(감마)이 m배로 커지므로 전략 성립 조건
    `수수료 APY > m × 풀레인지 IL`의 좌우변을 가르는 값이다.
    검증: ±10% -> 20.4 (Uniswap 공식 문서의 ~20x와 일치).
    """
    if lo_price <= 0 or hi_price <= lo_price:
        return 1.0
    denom = 1.0 - (lo_price / hi_price) ** 0.25
    return 1.0 / denom if denom > 0 else float("inf")


def concentration_from_pct(range_pct: float) -> float:
    """대칭 ±range_pct 레인지의 집중도 m. ±35% -> 5.99 (m<=3 아님)."""
    if range_pct <= 0 or range_pct >= 100:
        return 1.0
    return concentration_multiplier(1 - range_pct / 100, 1 + range_pct / 100)


def pct_for_concentration(m: float) -> float:
    """목표 집중도 m을 만족하는 대칭 레인지 반폭 %. m=3 -> ±67.0%."""
    if m <= 1:
        return 99.9
    r = (1 - 1 / m) ** 4
    return (1 - r) / (1 + r) * 100
