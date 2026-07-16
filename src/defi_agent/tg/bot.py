"""텔레그램 인터페이스 (@mjquant_bot).

명령: /status /pnl /pause /resume /events /start
알림: 액션(진입/재배치/재헤지/수수료), 경보(펀딩 역전/에러), 일일 리포트
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from ..config import Settings
from ..core.rebalancer import Rebalancer, CycleReport
from ..core.state import Store

log = logging.getLogger(__name__)


class TgInterface:
    def __init__(self, settings: Settings, rebalancer: Rebalancer, store: Store):
        self.s = settings
        self.rb = rebalancer
        self.store = store
        self.bot = Bot(token=settings.tg_bot_token) if settings.tg_bot_token else None
        self.dp = Dispatcher()
        self.last_report: CycleReport | None = None
        self._register()

    def _register(self):
        @self.dp.message(Command("start"))
        async def start(m: Message):
            await m.answer(
                f"defi-agent 연결됨 (chat_id: `{m.chat.id}`)\n"
                f"모드: {'DRY_RUN' if self.s.dry_run else '🔴 LIVE'}\n"
                "명령: /status /pnl /pause /resume /events", parse_mode="Markdown")

        @self.dp.message(Command("status"))
        async def status(m: Message):
            r = self.last_report
            if not r:
                await m.answer("아직 사이클 실행 전")
                return
            await m.answer(
                f"*상태* ({'DRY' if self.s.dry_run else 'LIVE'}"
                f"{', 일시정지' if self.rb.paused else ''})\n"
                f"ETH ${r.price:,.2f} · LP ${r.lp_value:,.0f} · 총자산 ${r.equity:,.0f}\n"
                f"LP델타 {r.lp_delta:.4f} / 숏 {r.hedge_size:.4f} ETH\n"
                f"레인지 위치 {r.range_ratio:.0%} · 펀딩(24h) {r.funding_apr:+.1f}% APR",
                parse_mode="Markdown")

        @self.dp.message(Command("pnl"))
        async def pnl(m: Message):
            now = int(time.time())
            series = await self.store.equity_series(now - 30 * 86400)
            if len(series) < 2:
                await m.answer("스냅샷 부족 (최소 2개 필요)")
                return
            e0, e1 = series[0][1], series[-1][1]
            days = max((series[-1][0] - series[0][0]) / 86400, 0.01)
            ret = (e1 / e0 - 1) * 100 if e0 else 0
            await m.answer(
                f"*PnL* (최근 {days:.1f}일)\n"
                f"${e0:,.2f} → ${e1:,.2f} ({ret:+.2f}%)\n"
                f"연환산 {ret / days * 365:+.1f}% APR", parse_mode="Markdown")

        @self.dp.message(Command("pause"))
        async def pause(m: Message):
            self.rb.paused = True
            await m.answer("⏸ 일시정지 — 신규 액션 중단, 관측은 계속")

        @self.dp.message(Command("resume"))
        async def resume(m: Message):
            self.rb.paused = False
            await m.answer("▶️ 재개")

        @self.dp.message(Command("events"))
        async def events(m: Message):
            rows = await self.store.recent_events(10)
            if not rows:
                await m.answer("이벤트 없음")
                return
            lines = [f"{time.strftime('%m-%d %H:%M', time.localtime(ts))} [{k}] {d}"
                     for ts, k, d in rows]
            await m.answer("\n".join(lines))

    async def notify(self, text: str):
        if not (self.bot and self.s.tg_chat_id):
            log.info("[TG 미설정] %s", text)
            return
        try:
            await self.bot.send_message(self.s.tg_chat_id, text, parse_mode="Markdown")
        except Exception:  # noqa: BLE001
            # 언더스코어 등으로 Markdown 파싱 실패 시 일반 텍스트로 재시도
            try:
                await self.bot.send_message(self.s.tg_chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("텔레그램 전송 실패")

    ALERT_COOLDOWN_SEC = 4 * 3600

    def _alert_key(self, text: str) -> str:
        """숫자·금액을 제거한 경보 유형 키 — 같은 유형은 쿨다운 내 재발송 안 함."""
        import re
        return re.sub(r"[\d\.,\$%+\-:]+", "", text)

    async def notify_cycle(self, r: CycleReport):
        self.last_report = r
        for a in r.actions:
            await self.notify(f"✅ {a}")
        now = time.time()
        if not hasattr(self, "_alert_last"):
            self._alert_last: dict[str, float] = {}
        for a in r.alerts:
            key = self._alert_key(a)
            if now - self._alert_last.get(key, 0) >= self.ALERT_COOLDOWN_SEC:
                self._alert_last[key] = now
                await self.notify(a)

    async def daily_report(self):
        now = int(time.time())
        series = await self.store.equity_series(now - 86400)
        r = self.last_report
        if not series or not r:
            return
        e0, e1 = series[0][1], series[-1][1]
        d = (e1 / e0 - 1) * 100 if e0 else 0
        await self.notify(
            f"📊 *일일 리포트*\n"
            f"총자산 ${e1:,.2f} (24h {d:+.2f}%)\n"
            f"ETH ${r.price:,.2f} · LP ${r.lp_value:,.0f} · 숏 {r.hedge_size:.4f}\n"
            f"레인지 {r.range_ratio:.0%} · 펀딩 {r.funding_apr:+.1f}% APR")

    async def run_polling(self):
        """명령 폴링 — 기본 비활성 (TG_POLLING=true일 때만).

        @mjquant_bot 토큰을 quant 봇과 공유하는 동안에는 폴링 금지:
        텔레그램 getUpdates는 나중에 요청한 쪽이 기존 연결을 끊는 구조라
        두 프로세스가 폴링하면 서로 무한히 끊어대며 양쪽 다 명령을 놓친다.
        (프로브 방식도 롱폴링 빈틈에 성공할 수 있어 신뢰 불가 — 실측 확인됨.)
        sendMessage(알림·리포트)는 폴링과 무관하게 항상 동작한다.
        명령이 필요하면: 전용 봇 토큰 발급 후 TG_POLLING=true, 또는
        quant 봇에 defi 핸들러 통합(권장, v0.2)."""
        if not self.bot:
            return
        import os
        if os.environ.get("TG_POLLING", "false").lower() != "true":
            log.info("TG 알림 전용 모드 (TG_POLLING=false) — 명령 폴링 안 함")
            return
        await self.dp.start_polling(self.bot, handle_signals=False)
