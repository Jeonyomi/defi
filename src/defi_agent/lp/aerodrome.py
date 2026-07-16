"""Aerodrome Slipstream WETH/USDC 포지션 관리.

토큰 정렬: Base에서 WETH(0x4200...) < USDC(0x8335...) 주소 순이므로
token0=WETH, token1=USDC. startup에서 실제 pool.token0()으로 재확인한다.
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
    price: float  # USDC per WETH


@dataclass
class Position:
    token_id: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    weth_amount: float   # 포지션 내 WETH (사람 단위)
    usdc_amount: float
    owed_weth: float     # 미수령 수수료
    owed_usdc: float
    range_ratio: float   # 0=중앙, >=1 레인지 이탈

    @property
    def value_usd(self) -> float:
        return self.usdc_amount + self.owed_usdc  # WETH분은 호출부에서 가격 곱해 합산


class AerodromeLP:
    def __init__(self, client: BaseClient):
        self.c = client
        self.s = client.s
        self.factory = client.contract(C.CL_FACTORY, C.FACTORY_ABI)
        self.npm = client.contract(C.NPM, C.NPM_ABI)
        self.router = client.contract(C.SWAP_ROUTER, C.ROUTER_ABI)
        self.weth = client.contract(C.WETH, C.ERC20_ABI)
        self.usdc = client.contract(C.USDC, C.ERC20_ABI)
        self.pool_address: str | None = None

    # ── 조회 ────────────────────────────────────────────────
    def startup_verify(self) -> str:
        """factory에서 풀 해석 + 주소들 코드 존재 확인. 실패 시 기동 중단."""
        pool = self.factory.functions.getPool(
            Web3.to_checksum_address(C.WETH), Web3.to_checksum_address(C.USDC),
            self.s.lp_tick_spacing).call()
        if int(pool, 16) == 0:
            raise RuntimeError(f"풀 없음: tickSpacing={self.s.lp_tick_spacing}")
        pool_c = self.c.contract(pool, C.POOL_ABI)
        t0 = pool_c.functions.token0().call()
        if t0.lower() != C.WETH.lower():
            raise RuntimeError(f"token0이 WETH가 아님: {t0} — 정렬 가정 위반, 코드 수정 필요")
        for name, addr in [("NPM", C.NPM), ("SwapRouter", C.SWAP_ROUTER)]:
            if not self.c.has_code(addr):
                raise RuntimeError(f"{name} 주소에 코드 없음: {addr} — constants.py 재검증 필요")
        self.pool_address = pool
        log.info("startup_verify OK: pool=%s tickSpacing=%d", pool, self.s.lp_tick_spacing)
        return pool

    def pool_state(self) -> PoolState:
        pool_c = self.c.contract(self.pool_address, C.POOL_ABI)
        slot0 = pool_c.functions.slot0().call()
        sqrt_p, tick = slot0[0], slot0[1]
        # token0=WETH(18), token1=USDC(6) → price = USDC per WETH
        price = clmath.sqrt_price_x96_to_price(sqrt_p, C.WETH_DECIMALS, C.USDC_DECIMALS)
        return PoolState(self.pool_address, sqrt_p, tick, price)

    def find_position(self) -> Position | None:
        """지갑이 보유한 이 풀·tickSpacing의 첫 포지션 반환."""
        n = self.npm.functions.balanceOf(self.c.address).call()
        st = self.pool_state()
        for i in range(n):
            tid = self.npm.functions.tokenOfOwnerByIndex(self.c.address, i).call()
            p = self.npm.functions.positions(tid).call()
            (_, _, t0, t1, spacing, tl, tu, liq, _, _, owed0, owed1) = p
            if (t0.lower(), t1.lower()) != (C.WETH.lower(), C.USDC.lower()):
                continue
            if spacing != self.s.lp_tick_spacing or liq == 0:
                continue
            a0, a1 = clmath.position_amounts(liq, st.sqrt_price_x96, tl, tu)
            return Position(
                token_id=tid, tick_lower=tl, tick_upper=tu, liquidity=liq,
                weth_amount=a0 / 10**C.WETH_DECIMALS, usdc_amount=a1 / 10**C.USDC_DECIMALS,
                owed_weth=owed0 / 10**C.WETH_DECIMALS, owed_usdc=owed1 / 10**C.USDC_DECIMALS,
                range_ratio=clmath.range_position_ratio(st.tick, tl, tu),
            )
        return None

    def wallet_balances(self) -> tuple[float, float]:
        w = self.weth.functions.balanceOf(self.c.address).call() / 10**C.WETH_DECIMALS
        u = self.usdc.functions.balanceOf(self.c.address).call() / 10**C.USDC_DECIMALS
        return w, u

    # ── 실행 ────────────────────────────────────────────────
    def _approve_if_needed(self, token, spender: str, amount_raw: int):
        """필요량만 approve — 무제한 승인은 스펜더 컨트랙트 침해 시 지갑 전체가 노출된다."""
        cur = token.functions.allowance(self.c.address, Web3.to_checksum_address(spender)).call()
        if cur < amount_raw:
            self.c.send(token.functions.approve(Web3.to_checksum_address(spender), amount_raw))

    def swap(self, token_in: str, amount_in_raw: int, min_out_raw: int) -> str | None:
        token_out = C.USDC if token_in.lower() == C.WETH.lower() else C.WETH
        tok = self.weth if token_in.lower() == C.WETH.lower() else self.usdc
        self._approve_if_needed(tok, C.SWAP_ROUTER, amount_in_raw)
        params = (
            Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
            self.s.lp_tick_spacing, self.c.address, int(time.time()) + 300,
            amount_in_raw, min_out_raw, 0,
        )
        return self.c.send(self.router.functions.exactInputSingle(params))

    def prepare_ratio(self, usd_total: float, frac0: float, slippage: float = 0.005):
        """지갑 잔고를 usd_total × frac0 가치의 WETH / 나머지 USDC로 스왑 정렬."""
        st = self.pool_state()
        weth_bal, usdc_bal = self.wallet_balances()
        target_weth = usd_total * frac0 / st.price
        diff_weth = target_weth - weth_bal
        if abs(diff_weth) * st.price < 10:  # $10 미만 차이는 무시
            return
        if diff_weth > 0:  # USDC → WETH
            amount_in = int(diff_weth * st.price * 10**C.USDC_DECIMALS)
            min_out = int(diff_weth * (1 - slippage) * 10**C.WETH_DECIMALS)
            self.swap(C.USDC, amount_in, min_out)
        else:  # WETH → USDC
            amount_in = int(-diff_weth * 10**C.WETH_DECIMALS)
            min_out = int(-diff_weth * st.price * (1 - slippage) * 10**C.USDC_DECIMALS)
            self.swap(C.WETH, amount_in, min_out)

    def mint_centered(self, usd_total: float, slippage: float = 0.01) -> str | None:
        """현재가 중심 ±range_pct 레인지로 신규 포지션 mint."""
        if usd_total > self.s.lp_max_usdc:
            raise RuntimeError(f"LP_MAX_USDC 초과: {usd_total} > {self.s.lp_max_usdc}")
        st = self.pool_state()
        spacing = self.s.lp_tick_spacing
        lo_price = st.price * (1 - self.s.lp_range_pct / 100)
        hi_price = st.price * (1 + self.s.lp_range_pct / 100)
        # price = USDC/WETH 이고 tick은 token1/token0 raw 기준 → 그대로 변환
        tl = clmath.align_tick(clmath.price_to_tick(lo_price, C.WETH_DECIMALS, C.USDC_DECIMALS), spacing)
        tu = clmath.align_tick(clmath.price_to_tick(hi_price, C.WETH_DECIMALS, C.USDC_DECIMALS), spacing)
        if tl >= tu:
            raise RuntimeError("레인지 계산 오류")
        # 정렬된 tick 기준 정확한 필요 수량 (±% 근사 금지 — tick 정렬로 경계가 밀려
        # 구성비가 수 % 어긋나면 mint의 PSC 체크에 걸린다)
        da0, da1, frac0 = clmath.amounts_for_budget(
            st.sqrt_price_x96, tl, tu, int(usd_total * 10**C.USDC_DECIMALS))
        self.prepare_ratio(usd_total, frac0)
        weth_raw = self.weth.functions.balanceOf(self.c.address).call()
        usdc_raw = self.usdc.functions.balanceOf(self.c.address).call()
        # 지갑 잔고가 필요량에 못 미치면 양쪽을 같은 비율로 축소 (비율 유지가 핵심)
        scale = min(1.0,
                    weth_raw / da0 if da0 else 1.0,
                    usdc_raw / da1 if da1 else 1.0)
        a0, a1 = int(da0 * scale), int(da1 * scale)
        self._approve_if_needed(self.weth, C.NPM, a0)
        self._approve_if_needed(self.usdc, C.NPM, a1)
        # 비율이 정확하므로 min 허용치는 블록 간 가격 드리프트만 흡수하면 됨
        min_tol = max(slippage, 0.02)
        params = (
            Web3.to_checksum_address(C.WETH), Web3.to_checksum_address(C.USDC),
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
