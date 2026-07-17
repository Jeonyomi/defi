"""텔레그램 인터페이스 (@mjquant_bot — quant 봇과 토큰 공유).

이 프로세스는 명령을 받지 못한다. 텔레그램 getUpdates는 단일 소비자만
허용하고 그 자리를 quant 봇이 점유하기 때문이다 (run_polling 참조).
따라서 사용자 명령은 quant의 /lp* 핸들러가 처리하고, 이 클래스는
매 사이클 렌더링된 본문을 DB(kv.status_text)에 발행해 그쪽에 넘긴다.
아래 명령 핸들러는 TG_POLLING=true인 전용 봇 구성에서만 살아난다.

명령(전용 봇 한정): /status /pnl /pause /resume /events /start
알림(항상 동작): 액션(진입/재배치/재헤지/수수료), 경보(펀딩 역전/에러),
      정기 상태(STATUS_NOTIFY_MIN 간격), 일일 리포트

메시지 형식은 _status_text/_action_text/_alert_text 세 곳에서만 만든다 —
/status·정기 상태·일일 리포트가 같은 본문을 공유하게 하기 위함.
"""
from __future__ import annotations

import datetime
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from ..config import Settings
from ..core.analytics import MIN_WINDOW_H, LpEdge, compute_edge
from ..core.rebalancer import Rebalancer, CycleReport
from ..core.state import Store
from ..lp.math import concentration_from_pct

log = logging.getLogger(__name__)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _mark(v: float, warn: float, crit: float) -> str:
    """수치를 신호등으로 — 임계값은 호출부가 설정에서 넘긴다."""
    return "🔴" if v >= crit else ("⚠️" if v >= warn else "✅")


def _kst(ts: float, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.datetime.fromtimestamp(ts, KST).strftime(fmt)


FLOW_JUMP_PCT = 0.05


def _trim_to_last_flow(series: list[tuple[int, float]]) -> tuple[list[tuple[int, float]], bool]:
    """입출금 지점 이후로 시계열을 자른다. 반환: (잘린 시계열, 자본변동 감지 여부).

    총자산 변화율을 그대로 수익률로 쓰면 입금이 수익으로 둔갑한다.
    실측 예: 14:33 HL 증거금 $59.66 입금으로 $200 → $259.66 (+29.8%).
    델타뉴트럴 북이 한 사이클(10분) 만에 5% 넘게 움직이는 건 시장 손익이
    아니라 입출금이므로, 마지막 점프 이후를 베이스라인으로 삼는다.

    한계: 한 사이클에 5%를 넘는 '진짜' 급손실도 자본변동으로 오인해
    수익률에서 빠진다. 이 규모(총자산 $260, 델타 ~0)에선 사실상 불가능하지만,
    자본이 커지거나 헤지가 끊기면 재검토할 것.
    """
    start = 0
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev > 0 and abs(cur / prev - 1) > FLOW_JUMP_PCT:
            start = i
    return series[start:], start > 0


class TgInterface:
    def __init__(self, settings: Settings, rebalancer: Rebalancer, store: Store):
        self.s = settings
        self.rb = rebalancer
        self.store = store
        self._vol_cache: tuple[float, tuple[float, int]] | None = None
        self.bot = Bot(token=settings.tg_bot_token) if settings.tg_bot_token else None
        self.dp = Dispatcher()
        self.last_report: CycleReport | None = None
        self._alert_last: dict[str, float] = {}
        self._status_last = 0.0
        self._register()

    def _mode(self) -> str:
        mode = "DRY_RUN" if self.s.dry_run else "🔴 LIVE"
        return mode + (" · ⏸ 일시정지" if self.rb.paused else "")

    async def _change(self, hours: float) -> tuple[float, float, bool] | None:
        """(변화율 %, 실제 관측 구간 h, 자본변동 여부). 데이터 부족이면 None.

        equity=0인 초기 스냅샷(입금·진입 전)은 제외한다 — 베이스라인이 0이면
        0으로 나누거나 '+0.00%'라는 거짓 수치가 나간다.
        요청 구간보다 데이터가 짧을 수 있으므로 실제 구간을 함께 돌려주고,
        호출부는 그 구간을 라벨에 그대로 표기한다 ('24h'라고 거짓말하지 않기 위해).
        """
        series = [(ts, e) for ts, e in
                  await self.store.equity_series(int(time.time() - hours * 3600)) if e > 0]
        series, flow = _trim_to_last_flow(series)
        if len(series) < 2:
            return None
        span_h = (series[-1][0] - series[0][0]) / 3600
        if span_h < 0.5:  # 표본 구간이 너무 짧으면 변화율이 노이즈
            return None
        return (series[-1][1] / series[0][1] - 1) * 100, span_h, flow

    @staticmethod
    def _chg_label(chg: tuple[float, float, bool] | None) -> str:
        if chg is None:
            return ""
        pct, span_h, flow = chg
        # 데이터가 24h에 못 미치면 '24h'라고 쓰지 않는다 (실제 구간을 표기)
        label = "24h" if span_h >= 20 else f"{span_h:.0f}h"
        if flow:  # 입출금으로 베이스라인이 리셋됐음을 숨기지 않는다
            label = "입금 이후 " + label
        return f"  _{label} {pct:+.2f}%_"

    def _vol_ref(self) -> tuple[float, int] | None:
        """30일 실현 변동성. 캔들 조회는 느리고 30일 sigma는 분 단위로 안 변하므로 1h 캐시."""
        now = time.time()
        if self._vol_cache and now - self._vol_cache[0] < 3600:
            return self._vol_cache[1]
        v = self.rb.hedge.realized_vol(days=30)
        if v[1] < 30:
            return self._vol_cache[1] if self._vol_cache else None
        self._vol_cache = (now, v)
        return v

    async def _edge(self) -> LpEdge | None:
        """LP 레그 경제성 (수수료 vs 감마손실). 실패해도 상태 표시를 막지 않는다."""
        try:
            rows = await self.store.edge_series(int(time.time()) - 7 * 24 * 3600)
            return compute_edge(rows, concentration_from_pct(self.s.lp_range_pct),
                                self._vol_ref())
        except Exception:
            return None

    @staticmethod
    def _edge_lines(e: LpEdge | None) -> list[str]:
        """LP가 돈을 벌고 있는지 한 블록으로.

        용어를 쓰지 않는다: 감마손실 -> '가격변동 손실', 커버리지 -> '몇 %를 메움',
        변동성/손익분기 -> '출렁임'/'버틸 수 있는 한계'. m은 판정과 무관하므로
        (부호가 m에 안 걸린다 — analytics 참조) 본문에서 뺐다.
        """
        if e is None:
            return []
        icon = {"positive": "✅", "negative": "🔴", "marginal": "🟡", "unknown": "⏳"}[e.verdict]
        head = {
            "positive": "수수료가 손실보다 큼 — 벌고 있음",
            "negative": "손실이 수수료보다 큼 — 잃고 있음",
            "marginal": "수수료와 손실이 비슷 — 판정 보류",
            "unknown": f"데이터 {e.window_h:.0f}시간 — 판정하려면 {MIN_WINDOW_H:.0f}시간 필요",
        }[e.verdict]
        out = [
            "",
            f"🧮 *LP 수지* {icon} _{e.window_h:.0f}시간 관측_",
            f"├ *{head}*",
        ] + TgInterface._coverage_lines(e) + [
            f"├ 1년 기준: 수수료 {e.fee_apr * 100:+.0f}% "
            f"− 가격변동 {e.gamma_apr * 100:.0f}% = *{e.net_apr * 100:+.0f}%*",
            f"├ ETH 출렁임 {e.vol * 100:.0f}% · 버틸 수 있는 한계 "
            f"{e.breakeven_vol * 100:.0f}%",
        ]
        if e.vol_src != "hl-30d":
            out.append("├ _출렁임 수치 불안정 — 참고만_")
        if e.verdict == "negative":
            out.append("└ _범위를 넓히거나 좁혀도 안 바뀜_")
        else:
            out[-1] = "└" + out[-1][1:]
        return out

    @staticmethod
    def _coverage_lines(e: LpEdge) -> list[str]:
        """실제로 번 돈 vs 잃은 돈. 모델(APR 추정)과 독립적인 실측 교차확인.

        경로의존적이라 APR로 안 바꾸고 누적 $와 비율로만 보여준다.
        """
        cov = e.coverage
        if cov is None:
            return [f"├ 수수료 ${e.fee_usd:.4f} · 가격변동 손실 없음 "
                    f"_(ETH {e.px_chg * 100:+.1f}%)_"]
        return [
            f"├ 번 돈 ${e.fee_usd:.4f} · 잃은 돈 ${abs(e.il_usd):.4f}",
            f"├ 수수료가 손실의 *{cov * 100:.0f}%* 를 메움 _(100%면 본전)_",
            f"├ _ETH {e.px_chg * 100:+.1f}% — 되돌아오면 손실도 사라짐_",
        ]

    def _status_text(self, r: CycleReport, chg: tuple[float, float, bool] | None = None,
                     title: str = "상태", edge: LpEdge | None = None) -> str:
        out = [
            f"📊 *{title}* · {self._mode()}",
            f"`{_kst(r.ts)} KST`",
            "",
            f"💰 *총자산 ${r.equity:,.2f}*{self._chg_label(chg)}",
            f"└ LP ${r.lp_value:,.2f} · 헤지 ${r.hl_account:,.2f} · 지갑 ${r.wallet_usd:,.2f}",
            "",
            f"📍 *안전 상태* · ETH ${r.price:,.2f}",
        ]
        if r.lp_value <= 0:
            out.append("└ LP 없음 — 진입 대기 중")
        else:
            # 레인지는 '남은 여유'로 뒤집어 보여준다 — 클수록 안전해야 직관적이다.
            room = 1 - r.range_ratio
            trigger_room = 1 - self.s.rerange_trigger
            # 드리프트(%)보다 '헤지 안 된 ETH가 몇 달러어치인가'가 바로 읽힌다.
            gap = abs(r.lp_delta - r.hedge_size)
            out += [
                f"├ 가격범위 여유 {room:.0%} "
                f"{_mark(r.range_ratio, 0.70, self.s.rerange_trigger)}"
                f" _({trigger_room:.0%} 남으면 재배치)_",
                f"├ 헤지 빈틈 {gap:.4f} ETH ≈ ${gap * r.price:,.0f} "
                f"_(LP {r.lp_delta:.4f} / 숏 {r.hedge_size:.4f})_",
                f"└ 레버리지 {r.eff_lev:.2f}x {_mark(r.eff_lev, 2.0, 2.5)} _(2.0x면 경보)_",
            ]
        out += [
            "",
            "💵 *들어오는 돈*",
            # 소액 구간에선 센트 반올림으로 누적이 안 보여 4자리까지 표기
            f"├ 쌓인 수수료 ${r.owed_usd:,.2f} _(아직 안 받음)_" if r.owed_usd >= 1
            else f"├ 쌓인 수수료 ${r.owed_usd:.4f} _(아직 안 받음)_",
            f"├ 펀딩 {r.funding_apr:+.1f}%/년 {'받는 중 ✅' if r.funding_apr >= 0 else '내는 중 ⚠️'}",
            f"└ 숏 평가손익 {'+' if r.hedge_upnl >= 0 else '-'}${abs(r.hedge_upnl):,.2f}",
        ]
        out += self._edge_lines(edge)
        return "\n".join(out)

    async def _pnl_text(self) -> str:
        # equity=0인 진입 전 스냅샷은 제외 — 베이스라인이 0이면 수익률이 무의미
        series = [(ts, e) for ts, e in
                  await self.store.equity_series(int(time.time()) - 30 * 86400) if e > 0]
        series, flow = _trim_to_last_flow(series)  # 입금을 수익으로 세지 않기 위해
        if len(series) < 2:
            return "스냅샷 부족 — 유효 스냅샷 2개 이상 필요"
        e0, e1 = series[0][1], series[-1][1]
        days = (series[-1][0] - series[0][0]) / 86400
        if days < 0.02:  # ~30분 미만이면 연환산이 무의미한 배율로 튄다
            return f"*PnL* 관측 구간 부족 ({days * 24:.1f}h)\n현재 총자산 ${e1:,.2f}"
        ret = (e1 / e0 - 1) * 100
        out = [f"📈 *LP 헤지 PnL* · 관측 {days:.1f}일",
               f"${e0:,.2f} → *${e1:,.2f}*  ({ret:+.2f}%)",
               f"연환산 {ret / days * 365:+.1f}% APR"]
        if flow:
            out.append("_마지막 입출금 이후 기준_")
        if days < 1:
            out.append("_표본 1일 미만 — 연환산은 참고용_")
        return "\n".join(out)

    def _action_text(self, a: str, r: CycleReport) -> str:
        return (f"✅ *실행됨* · `{_kst(r.ts, '%H:%M')} KST`\n{a}\n"
                f"_총자산 ${r.equity:,.2f} · ETH ${r.price:,.2f}_")

    def _alert_text(self, a: str, r: CycleReport) -> str:
        # 쿨다운 시간은 붙이지 않는다 — 알림 시스템의 내부 사정이지 사용자가 할 일이 아니다.
        head = a if a.startswith(("⚠️", "🚨", "⏸", "⏳", "💤")) else "⚠️ " + a
        return f"{head}\n`{_kst(r.ts, '%H:%M')} KST`"

    def _allowed(self, m: Message) -> bool:
        """TG_CHAT_ID 화이트리스트 — 다른 사용자의 명령은 조용히 무시."""
        return str(m.chat.id) == str(self.s.tg_chat_id)

    def _register(self):
        @self.dp.message(Command("start"))
        async def start(m: Message):
            if not self._allowed(m):
                return
            await m.answer(
                f"defi-agent 연결됨 (chat_id: `{m.chat.id}`)\n"
                f"모드: {'DRY_RUN' if self.s.dry_run else '🔴 LIVE'}\n"
                "명령: /status /pnl /pause /resume /events\n"
                "_(전용 봇 구성 전용. @mjquant_bot 공유 시엔 /lp* 를 쓰세요)_",
                parse_mode="Markdown")

        @self.dp.message(Command("status"))
        async def status(m: Message):
            if not self._allowed(m):
                return
            r = self.last_report
            if not r:
                await m.answer("아직 사이클 실행 전")
                return
            await m.answer(self._status_text(r, await self._change(24), edge=await self._edge()),
                           parse_mode="Markdown")

        @self.dp.message(Command("pnl"))
        async def pnl(m: Message):
            if not self._allowed(m):
                return
            await m.answer(await self._pnl_text(), parse_mode="Markdown")

        @self.dp.message(Command("pause"))
        async def pause(m: Message):
            if not self._allowed(m):
                return
            self.rb.paused = True
            await self.store.set_kv("paused", "1")  # DB가 단일 소스 — run_cycle이 여기서 읽는다
            await m.answer("⏸ 일시정지 — 신규 액션 중단, 관측은 계속")

        @self.dp.message(Command("resume"))
        async def resume(m: Message):
            if not self._allowed(m):
                return
            self.rb.paused = False
            await self.store.set_kv("paused", "0")
            await m.answer("▶️ 재개")

        @self.dp.message(Command("events"))
        async def events(m: Message):
            if not self._allowed(m):
                return
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

    async def publish_status(self, r: CycleReport):
        """렌더링된 상태를 DB에 남긴다 — quant 봇의 /lp가 이걸 읽어 전달한다.

        본문을 그대로 저장하는 이유: quant가 원자료로 다시 그리면 표현이 두 벌이 되어
        갈라진다. 렌더링은 이 클래스 한 곳에만 두고 quant는 전달만 한다.
        저장 실패가 사이클을 깨선 안 되므로 예외는 삼킨다 (표시 기능일 뿐).
        """
        try:
            await self.store.set_kv(
                "status_text",
                self._status_text(r, await self._change(24), title="LP 헤지 상태",
                                  edge=await self._edge()))
            await self.store.set_kv("pnl_text", await self._pnl_text())
        except Exception:  # noqa: BLE001
            log.exception("상태 발행 실패 — /lp가 직전 값을 보게 된다")

    async def notify_cycle(self, r: CycleReport):
        self.last_report = r
        await self.publish_status(r)
        for a in r.actions:
            await self.notify(self._action_text(a, r))
        now = time.time()
        for a in r.alerts:
            key = self._alert_key(a)
            if now - self._alert_last.get(key, 0) >= self.ALERT_COOLDOWN_SEC:
                self._alert_last[key] = now
                await self.notify(self._alert_text(a, r))
        await self._maybe_status(r)

    async def _maybe_status(self, r: CycleReport):
        """사이클에 얹어 보내는 정기 상태 알림.

        별도 타이머를 두지 않는 이유: 사이클이 죽으면 상태도 끊겨야
        '알림은 오는데 에이전트는 멈춘' 상태를 만들지 않는다.
        따라서 실제 간격은 REBALANCE_INTERVAL_SEC 단위로 올림된다.
        """
        every = self.s.status_notify_min * 60
        if every <= 0:  # 0 = 비활성
            return
        if r.ts - self._status_last < every:
            return
        self._status_last = r.ts
        await self.notify(self._status_text(r, await self._change(24), title="정기 상태",
                                            edge=await self._edge()))

    async def daily_report(self):
        r = self.last_report
        if not r:
            return
        self._status_last = r.ts  # 리포트 직후 정기 상태가 겹쳐 나가지 않도록
        await self.notify(self._status_text(r, await self._change(24), title="일일 리포트",
                                            edge=await self._edge()))

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
