"""2a686e3 이전에 기록된 '실행되지 않은 재헤지' 이벤트를 표시에서 걷어낸다.

배경: 조정분이 HL 최소주문($15) 미만이면 주문은 스킵되는데 액션 성공으로
기록되던 버그가 있었다 (2a686e3에서 수정). 그 결과 07-16 17:14~17:55에
'재헤지: 숏 0.0414 → ...' 이벤트 5건이 남았지만 숏은 한 번도 움직이지 않았다.
이 기록을 그대로 두면 이력을 볼 때마다 '재헤지가 자주 걸렸다'고 오독하게 된다.

지우지 않고 kind에 'stale:' 접두사를 붙이는 이유: 원본을 보존하면서 표시에서만
빼기 위함. 되돌리려면 접두사를 떼면 된다.

판정을 커밋 시각이나 rowid에 걸지 않고 스냅샷으로 직접 검증한다 — 이벤트가
주장하는 대로 숏이 실제로 움직였는지 확인하고, 안 움직인 것만 마킹한다.
사이클은 [스냅샷 → 액션] 순이므로 주문의 효과는 다음 사이클 스냅샷에 나타난다.

사용법: python scripts/mark_stale_hedge_events.py [--apply]
(기본은 dry-run — 무엇을 바꿀지 보여주기만 한다)
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys

DB = "defi_agent.db"
STALE_PREFIX = "stale:"
# 액션은 스냅샷 직후에 실행되므로 같은 사이클 스냅샷에는 안 잡힌다.
# 다음 사이클(10분 주기)의 스냅샷에서 확인한다.
EFFECT_LAG_S = 60


def _kst(ts: int) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M:%S")


def _executed(c: sqlite3.Connection, ts: int) -> bool | None:
    """이벤트가 주장하는 재헤지가 실제로 체결됐나. 판정 불가면 None."""
    before = c.execute(
        "SELECT hedge_size FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()
    after = c.execute(
        "SELECT hedge_size FROM snapshots WHERE ts > ? ORDER BY ts LIMIT 1",
        (ts + EFFECT_LAG_S,),
    ).fetchone()
    if not before or not after:
        return None
    return before[0] != after[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    rows = c.execute(
        "SELECT rowid, ts, kind, detail FROM events WHERE kind = 'hedge' "
        "AND detail LIKE '재헤지%' ORDER BY ts"
    ).fetchall()

    stale, real, unknown = [], [], []
    for rid, ts, _kind, detail in rows:
        ran = _executed(c, ts)
        (real if ran else unknown if ran is None else stale).append((rid, ts, detail))

    for label, group in (("실행됨 (유지)", real), ("판정불가 (유지)", unknown),
                         ("미실행 (마킹)", stale)):
        for rid, ts, detail in group:
            print(f"[{label}] {_kst(ts)} #{rid} {detail}")

    if not stale:
        print("\n마킹할 이벤트 없음 (이미 정리됐거나 해당 없음).")
        return 0

    if not args.apply:
        print(f"\ndry-run: {len(stale)}건이 마킹 대상. 반영하려면 --apply")
        return 0

    c.executemany(
        f"UPDATE events SET kind = '{STALE_PREFIX}' || kind WHERE rowid = ?",
        [(rid,) for rid, _, _ in stale],
    )
    c.commit()
    print(f"\n{len(stale)}건을 '{STALE_PREFIX}hedge'로 마킹했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
