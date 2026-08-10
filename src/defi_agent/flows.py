"""투입원금 원장 CLI — 누적손익(총자산 − 투입원금)의 분모를 관리한다.

총자산 변화율은 수익률이 아니다. 입금하면 총자산이 오르지만 번 돈은 아니고,
스냅샷 점프로 입출금을 추정하면 LP 재배치와 입금이 같은 사이클에 겹칠 때
금액이 어긋난다(2026-07-20 실측). 그래서 사람이 온체인·거래소 원장에서
확인한 실제 송금만 여기에 적고, 알림은 이 값을 원금으로 쓴다.

  python -m defi_agent.flows list
  python -m defi_agent.flows add --usd 200 --at "2026-07-16 13:52" --note "Base USDC"
  python -m defi_agent.flows rm  --at "2026-07-16 13:52"

--usd 는 입금 +, 출금 −. --at 은 KST, 생략하면 현재 시각.
가스용 네이티브 ETH는 총자산(equity)에 잡히지 않으므로 기본적으로 적지 않는다 —
적으면 자산은 빠진 채 원금만 늘어 손익이 그만큼 과소 표기된다.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime

from .config import load_settings
from .core.state import Store

KST = datetime.timezone(datetime.timedelta(hours=9))


def _parse_at(s: str | None) -> int:
    if not s:
        return int(datetime.datetime.now(KST).timestamp())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).replace(tzinfo=KST).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"--at 형식 오류: {s!r} (예: '2026-07-16 13:52')")


def _kst(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d %H:%M")


async def _run(a: argparse.Namespace):
    store = Store(load_settings().db_path)
    await store.init()

    if a.cmd == "add":
        await store.add_flow(_parse_at(a.at), a.usd, a.note)
    elif a.cmd == "rm":
        if not await store.del_flow(_parse_at(a.at)):
            raise SystemExit(f"해당 시각의 기록 없음: {a.at}")

    rows = await store.flows()
    if not rows:
        print("원장 비어 있음 — 누적손익은 '미등록'으로 표시된다")
        return
    for ts, usd, note in rows:
        print(f"{_kst(ts)}  {usd:>+10.2f}  {note}")
    total = sum(u for _, u, _ in rows)
    days = (datetime.datetime.now(KST).timestamp() - rows[0][0]) / 86400
    print(f"{'─' * 46}\n{'투입원금':<17}{total:>+10.2f}  ({len(rows)}건 · 운용 {days:.1f}일)")


def cli():
    p = argparse.ArgumentParser(prog="python -m defi_agent.flows", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("--usd", type=float, required=True, help="입금 +, 출금 −")
    add.add_argument("--at", help="KST 시각 (생략 시 현재)")
    add.add_argument("--note", default="")
    rm = sub.add_parser("rm")
    rm.add_argument("--at", required=True)
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    cli()
