"""Aerodrome Slipstream LP 포지션 관리 — LP_PAIR 설정 기반.

페어별 token0/token1·decimals는 constants.LP_PAIRS 프리셋에서 읽는다
(weth_usdc: WETH/USDC, cbeth_weth: cbETH/WETH). 주소 정렬 가정은
startup에서 실제 pool.token0()으로 재확인한다.

이 모듈의 price·수량은 전부 token0/token1 기준이다. USD 환산·델타 해석은
호출부(rebalancer) 몫 — price_is_usd=False 페어에서 price는 비율일 뿐이다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from web3 import Web3

from .. import constants as C
from ..chain.base_client import BaseClient
from . import math as clmath

log = logging.getLogger(__name__)

MAX_UINT128 = 2**128 - 1


@dataclass
class PoolState:
    pool: str
    sqrt_price_x96: int
    tick: int
    price: float  # token1 per token0 (weth_usdc: USDC/WETH=USD가, cbeth_weth: WETH/cbETH=비율)


@dataclass
class Position:
    token_id: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    amount0: float       # 포지션 내 token0 (사람 단위)
    amount1: float
    owed0: float         # 미수령 수수료
    owed1: float
    range_ratio: float   # 0=중앙, >=1 레인지 이탈

    # 호환 별칭 (weth_usdc 모드 명칭) — 호출부가 페어 인지로 정리되면 제거
    @property
    def weth_amount(self) -> float:
        return self.amount0

    @property
    def usdc_amount(self) -> float:
        return self.amount1

    @property
    def owed_weth(self) -> float:
        return self.owed0

    @property
    def owed_usdc(self) -> float:
        return self.owed1


class AerodromeLP:
    def __init__(self, client: BaseClient):
        self.c = client
        self.s = client.s
        self.pair = C.LP_PAIRS[self.s.lp_pair]
        self.t0_addr: str = self.pair["token0"]
        self.t1_addr: str = self.pair["token1"]
        self.dec0: int = self.pair["dec0"]
        self.dec1: int = self.pair["dec1"]
        self.factory = client.contract(C.CL_FACTORY, C.FACTORY_ABI)
        self.npm = client.contract(C.NPM, C.NPM_ABI)
        self.router = client.contract(C.SWAP_ROUTER, C.ROUTER_ABI)
        self.token0 = client.contract(self.t0_addr, C.ERC20_ABI)
        self.token1 = client.contract(self.t1_addr, C.ERC20_ABI)
        self.pool_address: str | None = None

    # ── 조회 ────────────────────────────────────────────────
    def startup_verify(self) -> str:
        """factory에서 풀 해석 + 주소들 코드 존재 확인. 실패 시 기동 중단."""
        pool = self.factory.functions.getPool(
            Web3.to_checksum_address(self.t0_addr), Web3.to_checksum_address(self.t1_addr),
            self.s.lp_tick_spacing).call()
        if int(pool, 16) == 0:
            raise RuntimeError(
                f"풀 없음: pair={self.s.lp_pair} tickSpacing={self.s.lp_tick_spacing}")
        pool_c = self.c.contract(pool, C.POOL_ABI)
        t0 = pool_c.functions.token0().call()
        if t0.lower() != self.t0_addr.lower():
            raise RuntimeError(
                f"token0 불일치: 풀={t0} 프리셋={self.t0_addr} — LP_PAIRS 정렬 가정 위반")
        for name, addr in [("NPM", C.NPM), ("SwapRouter", C.SWAP_ROUTER)]:
            if not self.c.has_code(addr):
                raise RuntimeError(f"{name} 주소에 코드 없음: {addr} — constants.py 재검증 필요")
        self.pool_address = pool
        log.info("startup_verify OK: pair=%s pool=%s tickSpacing=%d",
                 self.s.lp_pair, pool, self.s.lp_tick_spacing)
        return pool

    def pool_state(self) -> PoolState:
        pool_c = self.c.contract(self.pool_address, C.POOL_ABI)
        slot0 = pool_c.functions.slot0().call()
        sqrt_p, tick = slot0[0], slot0[1]
        price = clmath.sqrt_price_x96_to_price(sqrt_p, self.dec0, self.dec1)
        return PoolState(self.pool_address, sqrt_p, tick, price)

    def find_position(self) -> Position | None:
        """지갑이 보유한 이 풀·tickSpacing의 첫 포지션 반환."""
        n = self.npm.functions.balanceOf(self.c.address).call()
        st = self.pool_state()
        for i in range(n):
            tid = self.npm.functions.tokenOfOwnerByIndex(self.c.address, i).call()
            p = self.npm.functions.positions(tid).call()
            (_, _, t0, t1, spacing, tl, tu, liq, _, _, owed0, owed1) = p
            if (t0.lower(), t1.lower()) != (self.t0_addr.lower(), self.t1_addr.lower()):
                continue
            if spacing != self.s.lp_tick_spacing or liq == 0:
                continue
            a0, a1 = clmath.position_amounts(liq, st.sqrt_price_x96, tl, tu)
            sim = self.simulate_collect(tid)
            if sim is not None:
                owed0, owed1 = sim
            return Position(
                token_id=tid, tick_lower=tl, tick_upper=tu, liquidity=liq,
                amount0=a0 / 10**self.dec0, amount1=a1 / 10**self.dec1,
                owed0=owed0 / 10**self.dec0, owed1=owed1 / 10**self.dec1,
                range_ratio=clmath.range_position_ratio(st.tick, tl, tu),
            )
        return None

    def simulate_collect(self, token_id: int) -> tuple[int, int] | None:
        """실제 미수령 수수료를 staticcall로 조회. 실패 시 None.

        positions()의 tokensOwed는 mint/burn/collect로 포지션을 건드릴 때만 갱신되어
        보유 중에는 계속 0으로 읽힌다. NPM.collect는 내부에서 pool.burn(0) poke를
        먼저 수행하므로, eth_call로 시뮬레이션하면 상태 변경·가스 없이 실수치를 얻는다.
        """
        try:
            return self.npm.functions.collect((
                token_id, self.c.address, MAX_UINT128, MAX_UINT128)).call({"from": self.c.address})
        except Exception:  # noqa: BLE001
            log.warning("수수료 staticcall 실패 — positions() 값으로 대체", exc_info=True)
            return None

    def wallet_balances(self) -> tuple[float, float]:
        """지갑의 (token0, token1) 잔고 (사람 단위)."""
        b0 = self.token0.functions.balanceOf(self.c.address).call() / 10**self.dec0
        b1 = self.token1.functions.balanceOf(self.c.address).call() / 10**self.dec1
        return b0, b1

    # ── 실행 ────────────────────────────────────────────────
    def _approve_if_needed(self, token, spender: str, amount_raw: int):
        """필요량만 approve — 무제한 승인은 스펜더 컨트랙트 침해 시 지갑 전체가 노출된다."""
        cur = token.functions.allowance(self.c.address, Web3.to_checksum_address(spender)).call()
        if cur < amount_raw:
            self.c.send(token.functions.approve(Web3.to_checksum_address(spender), amount_raw))

    def swap(self, token_in: str, amount_in_raw: int, min_out_raw: int) -> str | None:
        """페어 두 토큰 간 스왑 (이 풀의 tickSpacing 사용)."""
        if token_in.lower() == self.t0_addr.lower():
            token_out, tok = self.t1_addr, self.token0
        elif token_in.lower() == self.t1_addr.lower():
            token_out, tok = self.t0_addr, self.token1
        else:
            raise RuntimeError(f"페어 밖 토큰 스왑 불가: {token_in} (pair={self.s.lp_pair})")
        self._approve_if_needed(tok, C.SWAP_ROUTER, amount_in_raw)
        params = (
            Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
            self.s.lp_tick_spacing, self.c.address, int(time.time()) + 300,
            amount_in_raw, min_out_raw, 0,
        )
        return self.c.send(self.router.functions.exactInputSingle(params))

    def _min_swap_t1(self) -> float:
        """무시할 스왑 하한 (token1 단위). USD 페어면 $10, WETH 쿼트면 ≈$10 상당."""
        return 10.0 if self.pair["price_is_usd"] else 0.0025

    def prepare_ratio(self, budget_t1: float, frac0: float, slippage: float = 0.005,
                      min_swap_t1: float | None = None):
        """지갑 잔고를 budget_t1(token1 단위) × frac0 가치의 token0 / 나머지 token1로 스왑 정렬."""
        st = self.pool_state()
        bal0, _bal1 = self.wallet_balances()
        target0 = budget_t1 * frac0 / st.price
        diff0 = target0 - bal0
        threshold = self._min_swap_t1() if min_swap_t1 is None else min_swap_t1
        if abs(diff0) * st.price < threshold:  # 미미한 차이는 무시
            return
        if diff0 > 0:  # token1 → token0
            amount_in = int(diff0 * st.price * 10**self.dec1)
            min_out = int(diff0 * (1 - slippage) * 10**self.dec0)
            self.swap(self.t1_addr, amount_in, min_out)
        else:  # token0 → token1
            amount_in = int(-diff0 * 10**self.dec0)
            min_out = int(-diff0 * st.price * (1 - slippage) * 10**self.dec1)
            self.swap(self.t0_addr, amount_in, min_out)

    def mint_centered(self, budget_t1: float, slippage: float = 0.01,
                      usd_value: float | None = None) -> str | None:
        """현재가 중심 ±range_pct 레인지로 신규 포지션 mint.

        budget_t1: token1 사람 단위 예산 (weth_usdc면 USDC≈USD, cbeth_weth면 WETH).
        usd_value: LP_MAX_USDC 상한 체크용 USD 가치 — None이면 budget_t1을 USD로 간주
        (price_is_usd 페어에서만 유효한 가정이므로 아닌 페어는 호출부가 반드시 넘길 것).
        """
        usd = budget_t1 if usd_value is None else usd_value
        if usd > self.s.lp_max_usdc:
            raise RuntimeError(f"LP_MAX_USDC 초과: {usd} > {self.s.lp_max_usdc}")
        st = self.pool_state()
        spacing = self.s.lp_tick_spacing
        lo_price = st.price * (1 - self.s.lp_range_pct / 100)
        hi_price = st.price * (1 + self.s.lp_range_pct / 100)
        # price = token1/token0 이고 tick도 같은 기준 → 그대로 변환
        tl = clmath.align_tick(clmath.price_to_tick(lo_price, self.dec0, self.dec1), spacing)
        tu = clmath.align_tick(clmath.price_to_tick(hi_price, self.dec0, self.dec1), spacing)
        if tl >= tu:
            raise RuntimeError("레인지 계산 오류")
        # 정렬된 tick 기준 정확한 필요 수량 (±% 근사 금지 — tick 정렬로 경계가 밀려
        # 구성비가 수 % 어긋나면 mint의 PSC 체크에 걸린다)
        da0, da1, frac0 = clmath.amounts_for_budget(
            st.sqrt_price_x96, tl, tu, int(budget_t1 * 10**self.dec1))
        self.prepare_ratio(budget_t1, frac0)
        bal0_raw = self.token0.functions.balanceOf(self.c.address).call()
        bal1_raw = self.token1.functions.balanceOf(self.c.address).call()
        # 지갑 잔고가 필요량에 못 미치면 양쪽을 같은 비율로 축소 (비율 유지가 핵심)
        scale = min(1.0,
                    bal0_raw / da0 if da0 else 1.0,
                    bal1_raw / da1 if da1 else 1.0)
        a0, a1 = int(da0 * scale), int(da1 * scale)
        self._approve_if_needed(self.token0, C.NPM, a0)
        self._approve_if_needed(self.token1, C.NPM, a1)
        # 비율이 정확하므로 min 허용치는 블록 간 가격 드리프트만 흡수하면 됨
        min_tol = max(slippage, 0.02)
        params = (
            Web3.to_checksum_address(self.t0_addr), Web3.to_checksum_address(self.t1_addr),
            spacing, tl, tu, a0, a1,
            int(a0 * (1 - min_tol)), int(a1 * (1 - min_tol)),
            self.c.address, int(time.time()) + 300, 0,
        )
        return self.c.send(self.npm.functions.mint(params))

    def close_position(self, pos: Position, slippage: float = 0.02) -> None:
        """유동성 전량 제거 + 수수료 수령 + NFT 소각."""
        st = self.pool_state()
        a0, a1 = clmath.position_amounts(pos.liquidity, st.sqrt_price_x96, pos.tick_lower, pos.tick_upper)
        self.c.send(self.npm.functions.decreaseLiquidity((
            pos.token_id, pos.liquidity,
            int(a0 * (1 - slippage)), int(a1 * (1 - slippage)),
            int(time.time()) + 300)))
        self.c.send(self.npm.functions.collect((
            pos.token_id, self.c.address, MAX_UINT128, MAX_UINT128)))
        self.c.send(self.npm.functions.burn(pos.token_id))

    def collect_fees(self, pos: Position) -> None:
        self.c.send(self.npm.functions.collect((
            pos.token_id, self.c.address, MAX_UINT128, MAX_UINT128)))
