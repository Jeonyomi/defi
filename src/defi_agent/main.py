"""defi-agent 진입점: 오케스트레이터.

기동 절차: 설정 로드 → 온체인 주소 검증 → 저장소 초기화 →
텔레그램 폴링 + 리밸런스 루프 + 일일 리포트 스케줄을 asyncio로 동시 실행.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

from .chain.base_client import BaseClient
from .config import load_settings
from .core.rebalancer import Rebalancer
from .core.state import Store
from .hedge.hyperliquid_client import HyperliquidHedge
from .lp.aerodrome import AerodromeLP
from .tg.bot import TgInterface

import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR.mkdir(exist_ok=True)
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8")])
log = logging.getLogger("defi_agent")

KST = datetime.timezone(datetime.timedelta(hours=9))


async def rebalance_loop(rb: Rebalancer, tg: TgInterface, interval: int):
    last_err_notify = 0.0
    while True:
        try:
            report = await rb.run_cycle()
            await tg.notify_cycle(report)
            log.info("cycle: equity=$%.2f lp=$%.2f delta=%.4f hedge=%.4f range=%.0f%%",
                     report.equity, report.lp_value, report.lp_delta,
                     report.hedge_size, report.range_ratio * 100)
        except Exception as e:  # noqa: BLE001
            log.exception("사이클 실패")
            # RPC 불안정 등으로 연속 실패해도 알림은 4시간에 1회만 (로그는 전부 남음)
            import time as _t
            if _t.time() - last_err_notify > 4 * 3600:
                last_err_notify = _t.time()
                await tg.notify(f"🚨 사이클 에러 (이후 동일 에러 알림은 4h 쿨다운): {e}")
        await asyncio.sleep(interval)


async def daily_report_loop(tg: TgInterface, hour_kst: int):
    while True:
        now = datetime.datetime.now(KST)
        target = now.replace(hour=hour_kst, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        await tg.daily_report()


def acquire_single_instance_lock():
    """같은 머신 이중 실행 방지 — 두 에이전트가 돌면 nonce 충돌 + 이중 헤지."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 47821))
    except OSError:
        raise SystemExit("defi-agent가 이미 실행 중입니다 (포트 47821 점유). "
                         "중복 실행은 nonce 충돌·이중 헤지를 일으킵니다.")
    return sock  # 참조 유지 필수 — GC되면 락 해제


async def amain():
    lock = acquire_single_instance_lock()  # noqa: F841 — 프로세스 수명 동안 유지
    s = load_settings()
    if not s.wallet_private_key:
        raise SystemExit("운용 지갑 키 없음 — `python -m defi_agent.keys set wallet` 먼저 실행")
    if not s.dry_run and not s.hl_api_private_key:
        raise SystemExit("LIVE 모드에 HL API 키 필요 — `python -m defi_agent.keys set hl-api`")
    log.info("defi-agent 기동 — 모드: %s", "DRY_RUN" if s.dry_run else "🔴 LIVE")

    client = BaseClient(s)
    lp = AerodromeLP(client)
    pool = lp.startup_verify()
    log.info("풀 검증 완료: %s", pool)

    hedge = HyperliquidHedge(s)
    store = Store(s.db_path)
    await store.init()

    rb = Rebalancer(s, lp, hedge, store)
    tg = TgInterface(s, rb, store)
    await tg.notify(f"🚀 defi-agent 기동 ({'DRY_RUN' if s.dry_run else 'LIVE'})\n"
                    f"풀: Aerodrome WETH/USDC `{pool[:10]}…`\n"
                    f"한도: ${s.lp_max_usdc:,.0f} · 레인지 ±{s.lp_range_pct}% · "
                    f"헤지 {s.hl_coin} ≤{s.hl_max_leverage}x")

    tasks = [
        asyncio.create_task(rebalance_loop(rb, tg, s.rebalance_interval_sec)),
        asyncio.create_task(daily_report_loop(tg, s.daily_report_hour_kst)),
    ]
    if tg.bot:
        tasks.append(asyncio.create_task(tg.run_polling()))
    await asyncio.gather(*tasks)


def cli():
    asyncio.run(amain())


if __name__ == "__main__":
    cli()
