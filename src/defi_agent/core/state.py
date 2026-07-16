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
    equity REAL                  -- 총자산 (USD)
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def log_event(self, kind: str, detail: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO events VALUES (?,?,?)", (int(time.time()), kind, detail))
            await db.commit()

    async def snapshot(self, **kw):
        cols = ("price lp_weth lp_usdc owed_weth owed_usdc hedge_size hedge_upnl "
                "hl_account wallet_weth wallet_usdc equity").split()
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

    async def recent_events(self, n: int = 10) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (n,))
            return await cur.fetchall()
