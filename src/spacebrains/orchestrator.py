"""Owns shared services (DB, brains, rate limiter) and the set of running agents."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from spacebrains.brain.jev import JevBrain
from spacebrains.brain.openrouter import OpenRouter, month_start_ts
from spacebrains.brain.strategist import Strategist
from spacebrains.config import Config
from spacebrains.db import Database
from spacebrains.events import EventBus
from spacebrains.game.agent import AgentContext
from spacebrains.game.world import World
from spacebrains.settings import AgentOverrides, Settings
from spacebrains.st.client import RateLimiter, STClient, STError

log = logging.getLogger("spacebrains.orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.bus = EventBus(self.db)
        self.limiter = RateLimiter()
        self.http = httpx.AsyncClient(timeout=30)
        self.router = OpenRouter(cfg.openrouter_api_key, cfg.openrouter_base_url, self.db)
        self.strategist = Strategist(self.router)
        self.world = World(self.db)
        self.settings = Settings()
        self.jev = JevBrain(cfg.typesafe_api_key, self.db, self.settings.jev_model)
        self.agents: dict[str, AgentContext] = {}
        self.account_client = STClient(
            cfg.spacetraders_base_url, cfg.spacetraders_api_key, self.limiter, self.http
        )

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.db.open()
        stored = await self.db.get_kv("settings")
        if stored:
            self.settings = Settings.model_validate({**Settings().model_dump(), **stored})
        self.jev.model = self.settings.jev_model
        for row in await self.db.list_agents():
            self._spawn(row)
        await self.bus.emit("system", f"orchestrator started with {len(self.agents)} agent(s)")

    async def stop(self) -> None:
        for a in list(self.agents.values()):
            await a.stop()
        await self.jev.aclose()
        await self.router.aclose()
        await self.http.aclose()
        await self.db.close()

    def _spawn(self, row: Any) -> AgentContext:
        client = STClient(
            self.cfg.spacetraders_base_url, row.token, self.limiter, self.http, key=row.symbol
        )
        ctx = AgentContext(
            row,
            client=client,
            db=self.db,
            bus=self.bus,
            world=self.world,
            jev=self.jev,
            strategist=self.strategist,
            global_settings=self.settings,
        )
        self.agents[row.symbol] = ctx
        ctx.start()
        return ctx

    # ---------------------------------------------------------------- settings
    async def update_settings(self, patch: dict[str, Any]) -> Settings:
        self.settings = Settings.model_validate({**self.settings.model_dump(), **patch})
        await self.db.set_kv("settings", self.settings.model_dump())
        self.jev.model = self.settings.jev_model
        for a in self.agents.values():
            a.update_settings(self.settings)
        await self.bus.emit("settings", f"settings updated: {', '.join(patch)}", data=patch)
        return self.settings

    async def update_agent_overrides(self, symbol: str, patch: dict[str, Any]) -> AgentOverrides:
        ctx = self.agents[symbol]
        merged = AgentOverrides.model_validate({**ctx.overrides.model_dump(), **patch})
        ctx.overrides = merged
        ctx.row.overrides = merged.model_dump()
        await self.db.set_agent_overrides(symbol, merged.model_dump())
        await self.bus.emit(
            "settings", f"overrides updated: {', '.join(patch)}", agent=symbol, data=patch
        )
        return merged

    async def set_agent_enabled(self, symbol: str, enabled: bool) -> None:
        ctx = self.agents[symbol]
        ctx.row.enabled = enabled
        await self.db.set_agent_enabled(symbol, enabled)
        await self.bus.emit(
            "settings", "agent enabled" if enabled else "agent disabled", agent=symbol
        )

    # ---------------------------------------------------------------- agents
    async def register_agent(self, symbol: str, faction: str) -> AgentContext:
        if len(self.agents) >= self.settings.max_agents:
            msg = f"max_agents ({self.settings.max_agents}) reached"
            raise ValueError(msg)
        symbol = symbol.upper()
        try:
            data = await self.account_client.register(symbol, faction)
        except STError as e:
            raise ValueError(e.message) from e
        agent = data["agent"]
        await self.db.add_agent(agent["symbol"], data["token"], faction, agent["headquarters"])
        rows = {r.symbol: r for r in await self.db.list_agents()}
        await self.bus.emit(
            "system", f"registered agent {agent['symbol']} ({faction})", agent=agent["symbol"]
        )
        return self._spawn(rows[agent["symbol"]])

    async def import_agent(self, token: str) -> AgentContext:
        client = STClient(self.cfg.spacetraders_base_url, token, self.limiter, self.http)
        try:
            agent = await client.my_agent()
        except STError as e:
            raise ValueError(e.message) from e
        if agent.symbol in self.agents:
            msg = f"{agent.symbol} already managed"
            raise ValueError(msg)
        await self.db.add_agent(agent.symbol, token, agent.starting_faction, agent.headquarters)
        rows = {r.symbol: r for r in await self.db.list_agents()}
        return self._spawn(rows[agent.symbol])

    async def remove_agent(self, symbol: str) -> None:
        ctx = self.agents.pop(symbol)
        await ctx.stop()
        await self.db.delete_agent(symbol)
        await self.bus.emit("system", f"removed agent {symbol}")

    async def replan(self, symbol: str) -> None:
        self.agents[symbol].request_replan("manual")

    # ---------------------------------------------------------------- views
    async def overview(self) -> dict[str, Any]:
        month = month_start_ts()
        return {
            "settings": self.settings.model_dump(),
            "agents": [a.snapshot() for a in self.agents.values()],
            "api_rate": {
                "limit_per_second": 2.0,
                "per_agent_last_minute": self.limiter.stats(60),
            },
            "usage": {
                "month_openrouter_usd": await self.db.usage_total("openrouter", month),
                "month_typesafe_usd": await self.db.usage_total("typesafe", month),
                "by_model": await self.db.usage_summary(month),
            },
        }
