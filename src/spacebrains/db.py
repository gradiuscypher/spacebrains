"""SQLite persistence: agents, settings, goals, events, LLM usage, market observations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    symbol TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    faction TEXT NOT NULL,
    headquarters TEXT NOT NULL,
    created_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    overrides TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    horizon TEXT NOT NULL,           -- 'long' | 'short'
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'active',  -- active | done | dropped
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS goals_agent ON goals(agent, status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT,
    ship TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_agent_ts ON events(agent, ts);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT,
    provider TEXT NOT NULL,          -- 'openrouter' | 'typesafe'
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage(ts);
CREATE TABLE IF NOT EXISTS markets (
    waypoint TEXT NOT NULL,
    good TEXT NOT NULL,
    buy INTEGER,                     -- what we pay to buy (purchasePrice)
    sell INTEGER,                    -- what we get when selling (sellPrice)
    supply TEXT,
    activity TEXT,
    volume INTEGER,
    kind TEXT NOT NULL,              -- export | import | exchange
    observed_at REAL NOT NULL,
    PRIMARY KEY (waypoint, good)
);
CREATE TABLE IF NOT EXISTS strategist_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    rounds INTEGER NOT NULL,
    models TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan TEXT NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    ship TEXT NOT NULL,
    good TEXT NOT NULL,
    buy_at TEXT NOT NULL,
    sell_at TEXT NOT NULL,
    units INTEGER NOT NULL,
    cost INTEGER NOT NULL,
    revenue INTEGER NOT NULL,
    predicted_margin INTEGER NOT NULL,
    seconds REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_agent ON trades(agent, ts);
CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    credits INTEGER NOT NULL,
    ships INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_agent_ts ON snapshots(agent, ts);
"""


@dataclass(slots=True)
class AgentRow:
    symbol: str
    token: str
    faction: str
    headquarters: str
    created_at: float
    enabled: bool
    overrides: dict[str, Any]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            msg = "database not open"
            raise RuntimeError(msg)
        return self._conn

    # --- kv / settings -------------------------------------------------
    async def get_kv(self, key: str) -> Any | None:
        async with self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else None

    async def set_kv(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await self.conn.commit()

    # --- agents --------------------------------------------------------
    async def add_agent(self, symbol: str, token: str, faction: str, headquarters: str) -> None:
        await self.conn.execute(
            "INSERT INTO agents(symbol,token,faction,headquarters,created_at) VALUES(?,?,?,?,?)",
            (symbol, token, faction, headquarters, time.time()),
        )
        await self.conn.commit()

    async def list_agents(self) -> list[AgentRow]:
        async with self.conn.execute("SELECT * FROM agents ORDER BY created_at") as cur:
            rows = await cur.fetchall()
        return [
            AgentRow(
                symbol=r["symbol"],
                token=r["token"],
                faction=r["faction"],
                headquarters=r["headquarters"],
                created_at=r["created_at"],
                enabled=bool(r["enabled"]),
                overrides=json.loads(r["overrides"]),
            )
            for r in rows
        ]

    async def set_agent_enabled(self, symbol: str, enabled: bool) -> None:
        await self.conn.execute(
            "UPDATE agents SET enabled=? WHERE symbol=?", (int(enabled), symbol)
        )
        await self.conn.commit()

    async def set_agent_overrides(self, symbol: str, overrides: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE agents SET overrides=? WHERE symbol=?", (json.dumps(overrides), symbol)
        )
        await self.conn.commit()

    async def delete_agent(self, symbol: str) -> None:
        await self.conn.execute("DELETE FROM agents WHERE symbol=?", (symbol,))
        await self.conn.execute("DELETE FROM goals WHERE agent=?", (symbol,))
        await self.conn.commit()

    # --- goals ---------------------------------------------------------
    async def replace_goals(self, agent: str, horizon: str, goals: list[dict[str, Any]]) -> None:
        now = time.time()
        await self.conn.execute(
            "UPDATE goals SET status='dropped', updated_at=? WHERE agent=? AND horizon=? AND status='active'",
            (now, agent, horizon),
        )
        await self.conn.executemany(
            "INSERT INTO goals(agent,horizon,kind,description,params,priority,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'active',?,?)",
            [
                (
                    agent,
                    horizon,
                    g["kind"],
                    g["description"],
                    json.dumps(g.get("params", {})),
                    int(g.get("priority", 5)),
                    now,
                    now,
                )
                for g in goals
            ],
        )
        await self.conn.commit()

    async def set_goal_status(self, goal_id: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=?", (status, time.time(), goal_id)
        )
        await self.conn.commit()

    async def list_goals(self, agent: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM goals WHERE agent=?"
        if not include_inactive:
            q += " AND status='active'"
        q += " ORDER BY priority ASC, id ASC"
        async with self.conn.execute(q, (agent,)) as cur:
            rows = await cur.fetchall()
        return [{**dict(r), "params": json.loads(r["params"])} for r in rows]

    # --- events --------------------------------------------------------
    async def add_event(
        self, kind: str, message: str, *, agent: str | None, ship: str | None, data: dict[str, Any]
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO events(ts,agent,ship,kind,message,data) VALUES(?,?,?,?,?,?)",
            (time.time(), agent, ship, kind, message, json.dumps(data)),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def list_events(
        self, agent: str | None = None, limit: int = 200, since_id: int = 0
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE id>?"
        params: list[Any] = [since_id]
        if agent:
            q += " AND agent=?"
            params.append(agent)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(q, params) as cur:
            rows = await cur.fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # --- llm usage -----------------------------------------------------
    async def add_usage(
        self,
        *,
        agent: str | None,
        provider: str,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO llm_usage(ts,agent,provider,model,purpose,input_tokens,output_tokens,cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), agent, provider, model, purpose, input_tokens, output_tokens, cost_usd),
        )
        await self.conn.commit()

    async def usage_summary(self, since_ts: float) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT provider, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd"
            " FROM llm_usage WHERE ts>=? GROUP BY provider, model ORDER BY cost_usd DESC",
            (since_ts,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def usage_total(self, provider: str, since_ts: float) -> float:
        async with self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_usage WHERE provider=? AND ts>=?",
            (provider, since_ts),
        ) as cur:
            row = await cur.fetchone()
        return float(row["c"]) if row else 0.0

    # --- markets -------------------------------------------------------
    async def upsert_market(self, waypoint: str, goods: list[dict[str, Any]]) -> None:
        now = time.time()
        await self.conn.executemany(
            "INSERT INTO markets(waypoint,good,buy,sell,supply,activity,volume,kind,observed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(waypoint,good) DO UPDATE SET buy=excluded.buy, sell=excluded.sell,"
            " supply=excluded.supply, activity=excluded.activity, volume=excluded.volume,"
            " kind=excluded.kind, observed_at=excluded.observed_at",
            [
                (
                    waypoint,
                    g["symbol"],
                    g.get("purchasePrice"),
                    g.get("sellPrice"),
                    g.get("supply"),
                    g.get("activity"),
                    g.get("tradeVolume"),
                    g["type"].lower(),
                    now,
                )
                for g in goods
            ],
        )
        await self.conn.commit()

    async def market_rows(self, waypoint_prefix: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM markets WHERE waypoint LIKE ? AND buy IS NOT NULL",
            (waypoint_prefix + "%",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def market_observed_at(self, waypoint_prefix: str) -> dict[str, float]:
        async with self.conn.execute(
            "SELECT waypoint, MAX(observed_at) AS t FROM markets WHERE waypoint LIKE ?"
            " AND buy IS NOT NULL GROUP BY waypoint",
            (waypoint_prefix + "%",),
        ) as cur:
            return {r["waypoint"]: float(r["t"]) for r in await cur.fetchall()}

    # --- strategist runs / snapshots -----------------------------------
    async def add_strategist_run(
        self,
        agent: str,
        rounds: int,
        models: list[str],
        summary: str,
        plan: dict[str, Any],
        cost: float,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO strategist_runs(ts,agent,rounds,models,summary,plan,cost_usd)"
            " VALUES(?,?,?,?,?,?,?)",
            (time.time(), agent, rounds, json.dumps(models), summary, json.dumps(plan), cost),
        )
        await self.conn.commit()

    async def list_strategist_runs(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM strategist_runs WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [
            {**dict(r), "models": json.loads(r["models"]), "plan": json.loads(r["plan"])}
            for r in rows
        ]

    async def add_trade(self, agent: str, **row: Any) -> None:
        cols = [
            "ship",
            "good",
            "buy_at",
            "sell_at",
            "units",
            "cost",
            "revenue",
            "predicted_margin",
            "seconds",
        ]
        await self.conn.execute(
            f"INSERT INTO trades(ts,agent,{','.join(cols)}) VALUES(?,?,{','.join('?' * len(cols))})",
            (time.time(), agent, *[row[c] for c in cols]),
        )
        await self.conn.commit()

    async def list_trades(self, agent: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM trades WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def add_snapshot(self, agent: str, credits: int, ships: int) -> None:
        await self.conn.execute(
            "INSERT INTO snapshots(ts,agent,credits,ships) VALUES(?,?,?,?)",
            (time.time(), agent, credits, ships),
        )
        await self.conn.commit()

    async def snapshots(self, agent: str, since_ts: float) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT ts,credits,ships FROM snapshots WHERE agent=? AND ts>=? ORDER BY ts",
            (agent, since_ts),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
