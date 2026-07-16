"""SQLite 저장소: 이벤트 로그 + 자산 스냅샷 (PnL 계산 기반)."""
from __future__ import annotations

import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- mint / rerange / hedge / collect / error / info
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts INTEGER PRIMARY KEY,
    price REAL, lp_weth REAL, lp_usdc REAL, owed_weth REAL, owed_usdc REAL,
    hedge_size REAL, hedge_upnl REAL, hl_account REAL,
    wallet_weth REAL, wallet_usdc REAL,
    equity REAL,                 -- 총자산 (USD)
    mark_px REAL                 -- HL mark price (외부 참조가, 변동성 계산용)
);
"""

# 기존 DB에 없는 컬럼을 덧붙인다 (ALTER는 중복 시 에러이므로 존재 확인 후 실행).
MIGRATIONS = {
    "mark_px": "ALTER TABLE snapshots ADD COLUMN mark_px REAL",
}


class Store:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            cur = await db.execute("PRAGMA table_info(snapshots)")
            have = {r[1] for r in await cur.fetchall()}
            for col, ddl in MIGRATIONS.items():
                if col not in have:
                    await db.execute(ddl)
            await db.commit()

    async def log_event(self, kind: str, detail: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO events VALUES (?,?,?)", (int(time.time()), kind, detail))
            await db.commit()

    async def snapshot(self, **kw):
        cols = ("price lp_weth lp_usdc owed_weth owed_usdc hedge_size hedge_upnl "
                "hl_account wallet_weth wallet_usdc equity mark_px").split()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"INSERT OR REPLACE INTO snapshots (ts,{','.join(cols)}) "
                f"VALUES ({','.join(['?'] * (len(cols) + 1))})",
                (int(time.time()), *[kw.get(c, 0.0) for c in cols]))
            await db.commit()

    async def equity_series(self, since_ts: int) -> list[tuple[int, float]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts, equity FROM snapshots WHERE ts >= ? ORDER BY ts", (since_ts,))
            return await cur.fetchall()

    async def edge_series(self, since_ts: int) -> list[tuple]:
        """LP 경제성 측정용 (ts, price, owed_weth, owed_usdc, lp_weth, lp_usdc, mark_px)."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts, price, owed_weth, owed_usdc, lp_weth, lp_usdc, mark_px "
                "FROM snapshots WHERE ts >= ? ORDER BY ts", (since_ts,))
            return await cur.fetchall()

    async def recent_events(self, n: int = 10) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (n,))
            return await cur.fetchall()
