"""cbETH/WETH 전환 수동 실행 (docs/MIGRATION_CBETH_WETH.md 실행 순서 2·4).

봇 정지 상태에서만 실행할 것 (포트 47821 락과 무관한 별도 프로세스 — nonce 충돌 방지).
서브커맨드:
  status          read-only 현재 상태 (구·신 풀, 지갑, HL) — tx 없음
  close           [tx] 구 WETH/USDC LP 전량 청산 (실행 순서 2)
  close-cbeth     [tx] 신 cbETH/WETH LP 전량 청산 (증액 재전개용)
  swap-to-weth    [tx] 구 풀에서 USDC→WETH: 지갑 ETH-eq를 TARGET_LP_USD까지 증액 (순서 4 전반)
  swap-to-cbeth   [tx] 신 풀에서 WETH→cbETH: ±2% mint 구성비(frac0)만큼 정렬 (순서 4 후반)
  sync-hedge      [tx-HL] 숏을 총델타(지갑+양풀 LP)에 즉시 수렴 — 수동 개입 후 갭 정리용

재배치 델타 규율: 모든 tx 서브커맨드는 끝에서 sync-hedge를 자동 수행한다.
(7/20 증액 전개에서 스왑 19:05 ~ 헤지 증액 19:09 사이 ~$290 롱이 4분 노출된 재발 방지 —
델타를 바꾸는 단계와 헤지 조정은 같은 프로세스 안에서 붙어 실행돼야 한다.)

TARGET_LP_USD는 env 오버라이드 가능 (기본 102). 증액 시: TARGET_LP_USD=395 python scripts/... swap-to-weth

각 tx 서브커맨드는 실행 전 계산 근거를 출력하고 실행 후 온체인 실측을 재출력한다.
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 자동진입 게이트(지갑 ETH-eq ≥ $100) 위에 가격 드리프트 버퍼 $2.
# lev = 목표/HL증거금 ≤ 1.67 (문서 예산 규칙 유지). 증액 시 env로 오버라이드.
TARGET_LP_USD = float(os.environ.get("TARGET_LP_USD", "102.0"))


def load(mode: str):
    """mode별 Settings — cbeth는 프로세스 env 오버라이드(.env 파일 무수정)."""
    if mode == "cbeth_weth":
        os.environ.update({"LP_PAIR": "cbeth_weth", "LP_TICK_SPACING": "1",
                           "LP_RANGE_PCT": "2", "LP_MAX_USDC": "110"})
    else:
        os.environ.update({"LP_PAIR": "weth_usdc", "LP_TICK_SPACING": "100",
                           "LP_RANGE_PCT": "35", "LP_MAX_USDC": "500"})
    from defi_agent.config import load_settings
    return load_settings()


def build(mode: str):
    from defi_agent.chain.base_client import BaseClient
    from defi_agent.lp.aerodrome import AerodromeLP
    s = load(mode)
    assert not s.dry_run, "이 스크립트는 LIVE 전환용 — DRY_RUN이면 .env 확인"
    c = BaseClient(s)
    lp = AerodromeLP(c)
    lp.startup_verify()
    return s, c, lp


def hl_state(s):
    from defi_agent.hedge.hyperliquid_client import HyperliquidHedge
    return HyperliquidHedge(s).state()


def read_wallet(c):
    from defi_agent import constants as C
    bals = {}
    for name, addr, dec in [("WETH", C.WETH, 18), ("USDC", C.USDC, 6), ("cbETH", C.CBETH, 18)]:
        bals[name] = c.contract(addr, C.ERC20_ABI).functions.balanceOf(c.address).call() / 10**dec
    return bals


def print_wallet(c, lp, label: str):
    eth = c.w3.eth.get_balance(c.address) / 1e18
    bals = read_wallet(c)
    print(f"[{label}] 지갑 {c.address}")
    print(f"  가스 ETH {eth:.6f} | WETH {bals['WETH']:.6f} | cbETH {bals['cbETH']:.6f} | USDC {bals['USDC']:.2f}")
    return bals


def measure_delta():
    """지갑 + 구·신 풀 LP의 총 ETH-eq 델타와 HL 상태를 실측 (read-only)."""
    s, c, lp = build("weth_usdc")
    pos = lp.find_position()
    old_delta = (pos.amount0 + pos.owed0) if pos else 0.0
    bals = read_wallet(c)
    s2, c2, lp2 = build("cbeth_weth")
    st2 = lp2.pool_state()
    pos2 = lp2.find_position()
    lp2_eq = ((pos2.amount0 + pos2.owed0) * st2.price + pos2.amount1 + pos2.owed1) if pos2 else 0.0
    delta = bals["WETH"] + bals["cbETH"] * st2.price + lp2_eq + old_delta
    return delta, hl_state(s2), s2


def sync_hedge():
    """숏을 총델타에 즉시 수렴. 델타를 바꾸는 모든 tx 서브커맨드가 끝에서 자동 호출한다."""
    from defi_agent.hedge.hyperliquid_client import HyperliquidHedge
    delta, hs, s = measure_delta()
    gap = (delta - hs.short_size) * hs.mark_px
    print(f"[헤지 동기화] 총델타 {delta:.4f} ETH vs 숏 {hs.short_size:.4f} → 갭 ${gap:+.1f}")
    if abs(gap) < 15:
        print("  갭 < $15 (HL 최소주문) — 조정 생략, 동기화 상태 유지")
        return
    HyperliquidHedge(s).set_target_short(delta)
    hs2 = hl_state(s)
    left = (delta - hs2.short_size) * hs2.mark_px
    print(f"  조정 완료: 숏 {hs2.short_size:.4f} ETH → 잔여 갭 ${left:+.1f}")


def cmd_status():
    s, c, lp = build("weth_usdc")
    st = lp.pool_state()
    pos = lp.find_position()
    print(f"[구 풀 weth_usdc] {st.pool} tick={st.tick} price=${st.price:.2f}")
    if pos:
        print(f"  포지션 #{pos.token_id}: WETH {pos.amount0:.6f} + USDC {pos.amount1:.2f} "
              f"(owed {pos.owed0:.6f}/{pos.owed1:.4f}) range={pos.range_ratio:.2f}")
    else:
        print("  포지션 없음")
    bals = print_wallet(c, lp, "지갑")
    hs = hl_state(s)
    print(f"[HL] 숏 {hs.short_size:.4f} ETH @ mark ${hs.mark_px:.2f} | 증거금 ${hs.account_value:.2f} "
          f"| uPnL ${hs.unrealized_pnl:.2f}")
    s2, c2, lp2 = build("cbeth_weth")
    st2 = lp2.pool_state()
    print(f"[신 풀 cbeth_weth] {st2.pool} tick={st2.tick} ratio={st2.price:.4f} WETH/cbETH")
    pos2 = lp2.find_position()
    if pos2:
        lp2_eq = (pos2.amount0 + pos2.owed0) * st2.price + pos2.amount1 + pos2.owed1
        print(f"  포지션 #{pos2.token_id}: cbETH {pos2.amount0:.6f} + WETH {pos2.amount1:.6f} "
              f"(ETH-eq {lp2_eq:.4f})")
    else:
        lp2_eq = 0.0
        print("  포지션 없음")
    eth_eq = (bals["WETH"] + bals["cbETH"] * st2.price + lp2_eq
              + (pos.amount0 + pos.owed0 if pos else 0))
    print(f"[델타] 지갑+LP ETH-eq {eth_eq:.4f} vs HL 숏 {hs.short_size:.4f} "
          f"→ 갭 ${abs(eth_eq - hs.short_size) * hs.mark_px:.1f}")


def cmd_close():
    s, c, lp = build("weth_usdc")
    pos = lp.find_position()
    assert pos, "청산할 weth_usdc 포지션 없음"
    print(f"청산 대상 #{pos.token_id}: WETH {pos.amount0:.6f} + USDC {pos.amount1:.2f} "
          f"+ owed({pos.owed0:.6f}, {pos.owed1:.4f})")
    print_wallet(c, lp, "청산 전")
    lp.close_position(pos)
    assert lp.find_position() is None, "청산 후에도 포지션이 조회됨"
    print("청산 완료 — NFT 소각까지 확인")
    print_wallet(c, lp, "청산 후")
    sync_hedge()


def cmd_close_cbeth():
    """증액 재전개용: 신 풀 cbETH/WETH 포지션 청산 (수수료 수거 포함)."""
    s, c, lp = build("cbeth_weth")
    pos = lp.find_position()
    assert pos, "청산할 cbeth_weth 포지션 없음"
    print(f"청산 대상 #{pos.token_id}: cbETH {pos.amount0:.6f} + WETH {pos.amount1:.6f} "
          f"+ owed({pos.owed0:.6f}, {pos.owed1:.6f})")
    print_wallet(c, lp, "청산 전")
    lp.close_position(pos)
    assert lp.find_position() is None, "청산 후에도 포지션이 조회됨"
    print("청산 완료 — NFT 소각까지 확인")
    print_wallet(c, lp, "청산 후")
    sync_hedge()


def cmd_swap_to_weth():
    s, c, lp = build("weth_usdc")
    st = lp.pool_state()
    bals = print_wallet(c, lp, "스왑 전")
    # 보유 cbETH도 ETH-eq에 포함 (증액 재전개 시 지갑에 cbETH가 남아있는 상태에서 호출됨)
    ratio = build("cbeth_weth")[2].pool_state().price if bals["cbETH"] > 1e-9 else 0.0
    target_eth = TARGET_LP_USD / st.price
    diff = target_eth - (bals["WETH"] + bals["cbETH"] * ratio)
    print(f"목표 {target_eth:.6f} ETH-eq (${TARGET_LP_USD} @ ${st.price:.2f}) — 부족분 {diff:.6f}")
    if diff * st.price < 1.0:
        print("부족분 $1 미만 — 스왑 생략")
        return
    amount_in = int(diff * st.price * 1e6)  # USDC 6dp
    min_out = int(diff * 0.995 * 1e18)
    assert bals["USDC"] >= amount_in / 1e6, "USDC 잔고 부족"
    lp.swap(lp.t1_addr, amount_in, min_out)  # USDC→WETH
    print_wallet(c, lp, "스왑 후")
    sync_hedge()  # 델타가 늘어난 직후 — 여기서 바로 숏 증액 (재배치 델타 규율)


def cmd_swap_to_cbeth():
    from defi_agent.lp import math as clmath
    s, c, lp = build("cbeth_weth")
    st = lp.pool_state()
    bals = print_wallet(c, lp, "스왑 전")
    budget_t1 = bals["WETH"] + bals["cbETH"] * st.price  # 총 ETH-eq (WETH 단위)
    spacing = s.lp_tick_spacing
    tl = clmath.align_tick(clmath.price_to_tick(st.price * (1 - s.lp_range_pct / 100), 18, 18), spacing)
    tu = clmath.align_tick(clmath.price_to_tick(st.price * (1 + s.lp_range_pct / 100), 18, 18), spacing)
    _a0, _a1, frac0 = clmath.amounts_for_budget(st.sqrt_price_x96, tl, tu, int(budget_t1 * 1e18))
    print(f"ratio={st.price:.4f} ticks[{tl},{tu}] frac0={frac0:.4f} budget={budget_t1:.6f} WETH")
    lp.prepare_ratio(budget_t1, frac0)  # WETH→cbETH (신 풀 spacing=1)
    bals2 = print_wallet(c, lp, "스왑 후")
    eth_eq = bals2["WETH"] + bals2["cbETH"] * st.price
    print(f"스왑 후 ETH-eq {eth_eq:.6f} (목표 구성비 cbETH {frac0:.1%})")
    sync_hedge()  # ETH-eq 불변 스왑이라 보통 no-op — 갭 검증 겸 안전망


def cmd_hold_cbeth():
    """cbETH 단순보유 전환 (2026-07-22 iter29 권고): 지갑 WETH 전량 → cbETH 스왑.

    LP 재진입 없음 — 실행 후 .env에 HOLD_MODE=1을 넣고 봇을 재기동하면
    rebalancer가 지갑 ETH-eq 델타 기준으로 헤지만 유지한다.
    ETH-eq 불변 스왑이라 sync-hedge는 보통 no-op — 갭 검증 겸 안전망.
    """
    s, c, lp = build("cbeth_weth")
    st = lp.pool_state()
    bals = print_wallet(c, lp, "스왑 전")
    weth = bals["WETH"]
    if weth * st.price < 0.0005:  # < ~$1 상당 — 스왑 실익 없음
        print(f"WETH 잔고 {weth:.6f} — 소액이라 스왑 생략")
        sync_hedge()
        return
    min_out = int(weth / st.price * 0.995 * 1e18)
    print(f"WETH {weth:.6f} 전량 → cbETH (비율 {st.price:.4f}, min_out {min_out / 1e18:.6f})")
    lp.swap(lp.t1_addr, int(weth * 1e18), min_out)  # WETH→cbETH
    print_wallet(c, lp, "스왑 후")
    sync_hedge()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"status": cmd_status, "close": cmd_close, "close-cbeth": cmd_close_cbeth,
     "swap-to-weth": cmd_swap_to_weth, "swap-to-cbeth": cmd_swap_to_cbeth,
     "hold-cbeth": cmd_hold_cbeth,
     "sync-hedge": sync_hedge}[cmd]()
