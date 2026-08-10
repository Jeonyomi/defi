"""SQLite 저장소: 이벤트 로그 + 자산 스냅샷 (PnL 계산 기반)."""
from __future__ import annotations

import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts INTEGER NOT NULL,
    -- mint / rerange / hedge / collect / error / info
    -- 'stale:' 접두사는 사실이 아닌 것으로 판명된 기록 (예: 2a686e3 이전의 허위 재헤지).
    -- 원본 보존을 위해 지우지 않고 표시에서만 뺀다 — scripts/mark_stale_hedge_events.py 참조.
    kind TEXT NOT NULL,
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
-- 프로세스 간 창구. @mjquant_bot 토큰을 quant 봇과 공유하는 탓에
-- 텔레그램 폴링은 quant 프로세스만 점유할 수 있다 (getUpdates는 단일 소비자).
-- 그래서 defi는 여기에 렌더링된 상태를 남기고, quant의 /lp가 읽어 전달한다.
-- 반대로 /lp_pause는 여기에 플래그를 쓰고, defi가 사이클 시작에 읽는다.
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    val TEXT NOT NULL
);
-- 투입원금 원장. 누적손익(= 총자산 - 투입원금)의 분모이자, 총자산 변화율로는
-- 절대 대체할 수 없는 값 — 입금은 총자산을 올리지만 수익이 아니기 때문이다.
-- 스냅샷 점프로 입출금을 추정하는 방법(_trim_to_last_flow)은 표시용 근사일 뿐,
-- 실제 07-20 이력에서 LP 재배치와 입금이 같은 사이클에 겹쳐 금액이 어긋났다.
-- 그래서 사람이 확인한 실제 송금만 여기에 적는다 (defi_agent.flows CLI).
CREATE TABLE IF NOT EXISTS flows (
    ts INTEGER PRIMARY KEY,   -- 송금 시각 (unix)
    usd REAL NOT NULL,        -- 입금 +, 출금 -
    note TEXT NOT NULL DEFAULT ''
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

    async def add_flow(self, ts: int, usd: float, note: str = ""):
        """입출금 1건 기록. 같은 ts는 덮어쓴다 (재입력으로 중복 계상되지 않게)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO flows (ts, usd, note) VALUES (?,?,?)",
                             (int(ts), float(usd), note))
            await db.commit()

    async def del_flow(self, ts: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM flows WHERE ts = ?", (int(ts),))
            await db.commit()
            return cur.rowcount

    async def flows(self) -> list[tuple[int, float, str]]:
        """시간순 입출금 원장. 비어 있으면 [] — 호출부는 '미등록'으로 표시해야 하며,
        0으로 간주해 총자산 전체를 수익으로 표기해선 안 된다."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT ts, usd, note FROM flows ORDER BY ts")
            return await cur.fetchall()

    async def set_kv(self, key: str, val: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO kv (key, ts, val) VALUES (?,?,?)",
                             (key, int(time.time()), val))
            await db.commit()

    async def get_kv(self, key: str) -> tuple[int, str] | None:
        """(기록 시각, 값). 없으면 None."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT ts, val FROM kv WHERE key = ?", (key,))
            return await cur.fetchone()

    async def recent_events(self, n: int = 10) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT ts, kind, detail FROM events WHERE kind NOT LIKE 'stale:%' "
                "ORDER BY ts DESC LIMIT ?", (n,))
            return await cur.fetchall()
