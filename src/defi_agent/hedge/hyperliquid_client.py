"""Hyperliquid 숏 헤지 레그. 공식 SDK 래퍼.

원칙:
- API wallet(에이전트 지갑) 키만 사용 — 메인 지갑 키는 절대 서버에 두지 않는다.
- 목표 숏 수량으로 수렴시키는 set_target_short()만 외부에 노출.
- DRY_RUN이면 주문 대신 로그.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants as hl_const
from eth_account import Account

from ..config import Settings

log = logging.getLogger(__name__)


@dataclass
class HedgeState:
    coin: str
    short_size: float        # 현재 숏 수량 (양수 = 숏)
    entry_px: float
    mark_px: float
    unrealized_pnl: float
    account_value: float
    leverage: float
    funding_apr_recent: float  # 최근 24h 펀딩 연환산 (양수 = 숏 수취)


class HyperliquidHedge:
    def __init__(self, settings: Settings):
        self.s = settings
        api_url = hl_const.TESTNET_API_URL if settings.hl_testnet else hl_const.MAINNET_API_URL
        if settings.hl_testnet:
            log.warning("HL_TESTNET=true — 헤지가 테스트넷에서 실행됨 (실 LP와 결합 금지)")
        self.info = Info(api_url, skip_ws=True)
        self.exchange = None
        if settings.hl_api_private_key and not settings.dry_run:
            wallet = Account.from_key(settings.hl_api_private_key)
            self.exchange = Exchange(wallet, api_url,
                                     account_address=settings.hl_account_address)

    def state(self) -> HedgeState:
        coin = self.s.hl_coin
        user = self.info.user_state(self.s.hl_account_address)
        mids = self.info.all_mids()
        mark = float(mids.get(coin, 0))
        short_size, entry, upnl, lev = 0.0, 0.0, 0.0, 0.0
        for ap in user.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin and float(pos.get("szi", 0)) != 0:
                szi = float(pos["szi"])
                short_size = -szi if szi < 0 else 0.0
                entry = float(pos.get("entryPx") or 0)
                upnl = float(pos.get("unrealizedPnl") or 0)
                lev = float(pos.get("leverage", {}).get("value") or 0)
        acct = float(user.get("marginSummary", {}).get("accountValue") or 0)
        funding = self._recent_funding_apr(coin)
        return HedgeState(coin, short_size, entry, mark, upnl, acct, lev, funding)

    def _recent_funding_apr(self, coin: str) -> float:
        import time
        try:
            hist = self.info.funding_history(coin, int((time.time() - 24 * 3600) * 1000))
            rates = [float(h["fundingRate"]) for h in hist]
            return sum(rates) / max(len(rates), 1) * 24 * 365 * 100
        except Exception:  # noqa: BLE001
            return 0.0

    def set_target_short(self, target_size: float) -> dict | None:
        """숏 수량을 target_size(코인 단위)로 수렴. 반환: 주문 결과 or None(변경 없음/DRY)."""
        st = self.state()
        diff = target_size - st.short_size  # +면 숏 늘림(매도), -면 줄임(매수)
        notional = abs(diff) * st.mark_px
        if notional < 15:  # HL 최소 주문 $10 + 버퍼
            return None
        # 레버리지 가드: 목표 노셔널이 계좌가치 × 최대레버리지 초과 금지
        if target_size * st.mark_px > st.account_value * self.s.hl_max_leverage:
            raise RuntimeError(
                f"헤지 레버리지 상한 초과: 목표 노셔널 ${target_size * st.mark_px:,.0f} > "
                f"계좌 ${st.account_value:,.0f} × {self.s.hl_max_leverage}x — 증거금 추가 필요")
        is_sell = diff > 0
        size = round(abs(diff), 4)
        if self.s.dry_run or self.exchange is None:
            log.info("[DRY_RUN] HL %s %s %.4f (노셔널 $%.0f)",
                     "숏 증가(매도)" if is_sell else "숏 축소(매수)", st.coin, size, notional)
            return None
        result = self.exchange.market_open(st.coin, not is_sell, size, None, 0.01)
        log.info("HL 주문 결과: %s", result)
        return result

    def close_all(self) -> dict | None:
        st = self.state()
        if st.short_size == 0:
            return None
        if self.s.dry_run or self.exchange is None:
            log.info("[DRY_RUN] HL 숏 전량 청산 %.4f %s", st.short_size, st.coin)
            return None
        return self.exchange.market_close(st.coin)
