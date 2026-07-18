"""전략 코어: 사이클마다 상태 평가 → 필요한 액션 실행.

사이클 로직 (우선순위 순):
1. 포지션 없음 + 자동진입 허용 → 50/50 정렬 후 mint + 헤지 설정
2. 레인지 이탈 임박/이탈 (range_ratio >= RERANGE_TRIGGER) → 쿨다운 확인 후 재배치
3. 델타 드리프트 (|헤지-LP델타|/LP델타 > HEDGE_DRIFT_PCT) → 재헤지
4. 미수령 수수료가 $50 초과 → collect
모든 액션은 이벤트로 기록하고 텔레그램 알림을 발행한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..hedge.hyperliquid_client import HyperliquidHedge
from ..lp.aerodrome import AerodromeLP
from ..core.state import Store

log = logging.getLogger(__name__)

# 온체인 리버트 코드는 4글자 약어라 그대로 보여주면 아무 의미가 없다.
# (Uniswap V3 계열 NonfungiblePositionManager / TransferHelper의 관례)
REVERT_HINTS = {
    "PSC": "슬리피지 초과 — 가격이 주문 도중 움직였습니다. 다음 사이클에 자동 재시도합니다",
    "STF": "토큰 전송 실패 — 잔고나 승인(approve)을 확인하세요",
    "IIA": "유동성 부족 — 넣으려는 금액이 너무 작습니다",
    "LOK": "풀이 잠겨 있습니다 — 다음 사이클에 자동 재시도합니다",
}
_ERR_MAX = 160


def _short_err(e: Exception) -> str:
    """예외를 한 줄로. 원문(헥스 덤프·트레이스백)은 로그에 남기고 알림엔 요약만 보낸다.

    web3는 리버트를 ('execution reverted: PSC', '0x08c379a0...200자') 같은 튜플로,
    RPC 오류는 {'code': -32000, 'message': '...'} 같은 dict로 던진다. 그대로 알림에
    실으면 사람이 읽을 수 없는 헥스가 대부분을 차지한다 (실측: 14:34 PSC 이벤트).
    """
    args = getattr(e, "args", None)
    raw = args[0] if args else e
    # web3 버전에 따라 리버트가 두 인자(message, data)로도, 튜플 한 개로도 온다.
    # 둘 다 str(e)는 "('execution reverted: PSC', '0x08c3…')"로 똑같이 보이므로 양쪽을 처리한다.
    while isinstance(raw, (tuple, list)) and raw:
        raw = raw[0]
    if isinstance(raw, dict):  # RPC 오류
        raw = raw.get("message", raw)
    text = str(raw).strip()
    for code, hint in REVERT_HINTS.items():
        if text.endswith(f": {code}") or text == code:
            return hint
    return text[:_ERR_MAX] + ("…" if len(text) > _ERR_MAX else "")


@dataclass
class CycleReport:
    ts: float
    price: float = 0.0
    equity: float = 0.0
    lp_value: float = 0.0
    hedge_size: float = 0.0
    lp_delta: float = 0.0
    range_ratio: float = 0.0
    funding_apr: float = 0.0
    owed_usd: float = 0.0       # 미수령 LP 수수료 (collect staticcall 실측)
    hl_account: float = 0.0     # HL 증거금
    hedge_upnl: float = 0.0
    wallet_usd: float = 0.0
    eff_lev: float = 0.0        # 헤지 노셔널 / 증거금
    drift_pct: float = 0.0      # |숏-LP델타| / LP델타
    paused: bool = False
    actions: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


class Rebalancer:
    def __init__(self, settings: Settings, lp: AerodromeLP, hedge: HyperliquidHedge, store: Store):
        self.s = settings
        self.lp = lp
        self.hedge = hedge
        self.store = store
        self.paused = False
        self._last_rerange_ts = 0.0

    async def _sync_paused(self):
        """일시정지 플래그를 DB에서 읽는다 — 이 프로세스는 텔레그램 명령을 못 받는다.

        토큰을 quant 봇과 공유해 폴링을 그쪽이 점유하므로(state.py kv 주석 참조),
        /lp_pause는 quant가 DB에 쓰고 여기서 읽는 방식으로만 닿는다.
        사이클 시작에 읽어도 지연이 없는 이유: 이 에이전트는 사이클 안에서만
        행동하므로 사이클 사이의 플래그 변화는 어차피 관측할 대상이 없다.
        """
        try:
            row = await self.store.get_kv("paused")
        except Exception:  # noqa: BLE001 — 플래그를 못 읽어도 사이클은 돌아야 한다
            log.exception("일시정지 플래그 조회 실패 — 직전 상태 유지")
            return
        if row is not None:
            self.paused = row[1] == "1"

    async def run_cycle(self) -> CycleReport:
        r = CycleReport(ts=time.time())
        await self._sync_paused()
        st = self.lp.pool_state()
        r.price = st.price
        pos = self.lp.find_position()
        hs = self.hedge.state()
        r.hedge_size = hs.short_size
        r.funding_apr = hs.funding_apr_recent
        wallet_weth, wallet_usdc = self.lp.wallet_balances()

        if pos:
            r.lp_delta = pos.weth_amount + pos.owed_weth
            r.lp_value = (pos.weth_amount + pos.owed_weth) * st.price + pos.usdc_amount + pos.owed_usdc
            r.range_ratio = pos.range_ratio
            r.owed_usd = pos.owed_weth * st.price + pos.owed_usdc
        r.wallet_usd = wallet_weth * st.price + wallet_usdc
        r.hl_account = hs.account_value
        r.hedge_upnl = hs.unrealized_pnl
        r.equity = r.lp_value + r.wallet_usd + hs.account_value
        if r.lp_delta > 0:
            r.drift_pct = abs(hs.short_size - r.lp_delta) / r.lp_delta * 100
        if hs.account_value > 0:
            r.eff_lev = hs.short_size * hs.mark_px / hs.account_value

        await self.store.snapshot(
            price=st.price,
            lp_weth=pos.weth_amount if pos else 0, lp_usdc=pos.usdc_amount if pos else 0,
            owed_weth=pos.owed_weth if pos else 0, owed_usdc=pos.owed_usdc if pos else 0,
            hedge_size=hs.short_size, hedge_upnl=hs.unrealized_pnl, hl_account=hs.account_value,
            wallet_weth=wallet_weth, wallet_usdc=wallet_usdc, equity=r.equity,
            mark_px=hs.mark_px)

        r.paused = self.paused
        if self.paused:
            # 알림을 내지 않는다 — 사용자가 직접 멈춘 상태이고 상태 머리말이
            # 이미 '⏸ 일시정지'를 보여준다. 매 사이클 알리면 순수 소음이다.
            log.info("일시정지 상태 — 관측만 수행")
            return r

        # 1) 신규 진입
        if pos is None:
            deployable = min(wallet_usdc + wallet_weth * st.price, self.s.lp_max_usdc)
            if deployable >= 100:
                # 헤지 레그 증거금 선확인 — 없으면 LP만 잡혀 단방향 노출이 되므로 진입 보류
                need_margin = deployable * 0.5 / self.s.hl_max_leverage * 1.2
                if hs.account_value < need_margin:
                    r.alerts.append(
                        f"⏸ LP 진입을 미뤘습니다 — 헤지할 돈이 부족합니다.\n"
                        f"헤지 잔고 ${hs.account_value:,.0f} · 필요 ${need_margin:,.0f}\n"
                        f"USDC를 넣어주시면 다음 사이클에 자동으로 들어갑니다.")
                    return r
                minted = await self._act(r, "mint",
                                         f"신규 LP 진입 ${deployable:,.0f} (±{self.s.lp_range_pct}%)",
                                         lambda: self.lp.mint_centered(deployable))
                if not minted:
                    return r
                pos = self._find_position_retry()
                # RPC 지연으로 포지션 조회가 늦어도 헤지는 반드시 건다 —
                # ±35% 레인지의 WETH 가치비율 ~0.42로 추정 (언헤지 방치가 더 위험)
                target = (pos.weth_amount + pos.owed_weth) if pos \
                    else deployable * 0.42 / st.price
                await self._act(r, "hedge", f"초기 헤지 숏 {target:.4f} ETH",
                                lambda: self.hedge.set_target_short(target))
            else:
                r.alerts.append(f"💤 놀고 있습니다 — LP도 없고 넣을 돈도 없습니다 "
                                f"(지갑 ${deployable:,.0f}, 최소 $100 필요).")
            return r

        # 2) 레인지 재배치
        if pos.range_ratio >= self.s.rerange_trigger:
            cooldown_ok = time.time() - self._last_rerange_ts > self.s.rerange_cooldown_h * 3600
            if cooldown_ok:
                usd = r.lp_value
                await self._act(r, "rerange",
                                f"레인지 재배치 (ratio {pos.range_ratio:.2f}, ${usd:,.0f})",
                                lambda: self._do_rerange(pos, usd))
                self._last_rerange_ts = time.time()
                return r
            wait_h = self.s.rerange_cooldown_h - (time.time() - self._last_rerange_ts) / 3600
            r.alerts.append(f"⏳ 가격범위를 옮겨야 하는데 대기 중입니다 "
                            f"(연속 재배치 방지, {max(wait_h, 0):.0f}시간 뒤 실행).")

        # 3) 델타 드리프트 재헤지
        if r.lp_delta > 0:
            drift = r.drift_pct
            if drift > self.s.hedge_drift_pct:
                adj_notional = abs(r.lp_delta - hs.short_size) * hs.mark_px
                if adj_notional < 16:  # set_target_short의 $15 스킵 임계 + 가격차 버퍼
                    # 알리지 않는다 — 설계대로의 정상 동작이고 사용자가 할 일이 없다.
                    # 노출 규모는 상태의 '헤지 빈틈' 줄이 이미 달러로 보여준다.
                    log.info("재헤지 보류: 조정분 $%.0f < HL 최소주문 $15 (드리프트 %.1f%%)",
                             adj_notional, drift)
                else:
                    await self._act(r, "hedge",
                                    f"재헤지: 숏 {hs.short_size:.4f} → {r.lp_delta:.4f} ETH (드리프트 {drift:.1f}%)",
                                    lambda: self.hedge.set_target_short(r.lp_delta))
        elif hs.short_size > 0:
            # LP가 레인지를 완전히 벗어나 ETH를 전량 USDC로 바꾼 상태(lp_delta==0)인데
            # 숏이 남아 있으면 '벌거벗은 숏' — 상승장에서 순수 방향 손실이 된다.
            # 재배치 쿨다운으로 재중심을 못 잡는 구간에서도 델타를 0으로 유지하도록 숏을 걷어낸다.
            # 가격이 레인지로 되돌아오면 LP가 ETH를 되사고 다음 사이클(10분)에 재헤지된다.
            # (하단 완전 이탈은 lp_delta가 최대치라 위 정상 경로가 처리하므로, 이 분기는 상단 이탈만 탄다.)
            naked_notional = hs.short_size * hs.mark_px
            if naked_notional >= 15:  # HL 최소주문 미만이면 노출도 $15 미만이라 다음 사이클로 보류
                await self._act(r, "hedge",
                                f"헤지 축소: 숏 {hs.short_size:.4f} → 0 ETH (LP 레인지 완전 이탈)",
                                lambda: self.hedge.close_all())
            else:
                log.info("레인지 완전 이탈 + 잔여 숏 $%.0f < HL 최소주문 — 보류", naked_notional)

        # 4) 수수료 수령
        owed_usd = r.owed_usd
        if owed_usd > 50:
            await self._act(r, "collect", f"수수료 수령 ${owed_usd:.2f}",
                            lambda: self.lp.collect_fees(pos))

        # 경보: 펀딩 역전 — 받던 돈을 내기 시작하면 수익원이 하나 사라진다
        if hs.funding_apr_recent < -3:
            r.alerts.append(f"⚠️ 펀딩이 뒤집혔습니다 — 이제 받는 게 아니라 "
                            f"연 {abs(hs.funding_apr_recent):.1f}%씩 내는 중입니다.")

        # 경보: 증거금 부족 (예산 규칙: HL 증거금 >= 헤지 노셔널의 60% = LP의 30%)
        notional = hs.short_size * hs.mark_px
        if notional > 0 and hs.account_value > 0:
            eff_lev = r.eff_lev
            if eff_lev > 2.5:
                r.alerts.append(
                    f"🚨 헤지 잔고가 부족합니다 — 레버리지 {eff_lev:.1f}x "
                    f"(잔고 ${hs.account_value:,.0f}로 ${notional:,.0f}어치를 잡고 있음).\n"
                    f"USDC를 넣어주세요. 이대로 두면 청산 위험이 커집니다.")
            elif eff_lev > 2.0:
                r.alerts.append(f"⚠️ 레버리지가 {eff_lev:.1f}x까지 올랐습니다 "
                                f"(2.5x 넘으면 위험). USDC 보충을 권합니다.")
        return r

    async def _act(self, r: CycleReport, kind: str, desc: str, fn) -> bool:
        """액션 실행 + 기록. 성공 여부를 반환해 호출부가 플로우를 제어할 수 있게 한다."""
        prefix = "[DRY_RUN] " if self.s.dry_run else ""
        try:
            fn()
            r.actions.append(prefix + desc)
            await self.store.log_event(kind, prefix + desc)
            return True
        except Exception as e:  # noqa: BLE001
            log.exception("%s 실패: %r", desc, e)  # 원문·트레이스백은 로그에만
            msg = f"{desc} 실패 — {_short_err(e)}"
            r.alerts.append("🚨 " + msg)
            await self.store.log_event("error", msg)
            return False

    def _find_position_retry(self, tries: int = 3, wait_s: float = 2.0):
        """mint 직후 RPC 노드 지연으로 포지션이 안 보일 수 있어 재시도."""
        for i in range(tries):
            pos = self.lp.find_position()
            if pos:
                return pos
            if i < tries - 1:
                time.sleep(wait_s)
        return None

    def _do_rerange(self, pos, usd_total: float):
        self.lp.close_position(pos)
        self.lp.mint_centered(min(usd_total, self.s.lp_max_usdc))
        new_pos = self.lp.find_position()
        if new_pos:
            self.hedge.set_target_short(new_pos.weth_amount + new_pos.owed_weth)
