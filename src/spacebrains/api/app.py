"""FastAPI app: JSON API + SSE event stream + static frontend."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from spacebrains.config import Config
from spacebrains.orchestrator import Orchestrator
from spacebrains.settings import AgentOverrides, Settings

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class RegisterBody(BaseModel):
    symbol: str
    faction: str = "COSMIC"


class ImportBody(BaseModel):
    token: str


class EnabledBody(BaseModel):
    enabled: bool


def create_app(cfg: Config) -> FastAPI:
    orch = Orchestrator(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await orch.start()
        try:
            yield
        finally:
            await orch.stop()

    app = FastAPI(title="spacebrains", lifespan=lifespan)
    app.state.orch = orch

    # ------------------------------------------------------------ overview
    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return await orch.overview()

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {"values": orch.settings.model_dump(), "schema": settings_schema()}

    @app.patch("/api/settings")
    async def patch_settings(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            return (await orch.update_settings(patch)).model_dump()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/usage")
    async def usage() -> dict[str, Any]:
        month = time.time() - 31 * 86400
        return {
            "by_model": await orch.db.usage_summary(month),
            "openrouter_key": await orch.router.key_status(),
        }

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        """Cheap OpenRouter models to offer in the settings dropdowns."""
        resp = await orch.http.get(cfg.openrouter_base_url.rstrip("/") + "/models")
        resp.raise_for_status()
        out = []
        for m in resp.json().get("data", []):
            p = m.get("pricing", {})
            try:
                pi, po = float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6
            except (TypeError, ValueError):
                continue
            if ":free" in m["id"] or pi > 5:
                continue
            out.append(
                {"id": m["id"], "prompt_per_m": round(pi, 3), "completion_per_m": round(po, 3)}
            )
        out.sort(key=lambda x: x["prompt_per_m"])
        return {"models": out}

    @app.post("/api/reset/acknowledge")
    async def ack_reset() -> dict[str, str]:
        await orch.acknowledge_reset()
        return {"ok": "1"}

    # ------------------------------------------------------------ agents
    @app.post("/api/agents")
    async def register(body: RegisterBody) -> dict[str, Any]:
        try:
            ctx = await orch.register_agent(body.symbol, body.faction)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return ctx.snapshot()

    @app.post("/api/agents/import")
    async def import_agent(body: ImportBody) -> dict[str, Any]:
        try:
            ctx = await orch.import_agent(body.token)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return ctx.snapshot()

    @app.get("/api/agents/{symbol}")
    async def agent_detail(symbol: str) -> dict[str, Any]:
        ctx = _agent(orch, symbol)
        return {
            **ctx.snapshot(),
            "goals": await orch.db.list_goals(symbol, include_inactive=True),
            "strategist_runs": await orch.db.list_strategist_runs(symbol, 8),
            "trades": await orch.db.list_trades(symbol, 30),
            "snapshots": await orch.db.snapshots(symbol, time.time() - 24 * 3600),
            "overrides_schema": overrides_schema(),
        }

    @app.patch("/api/agents/{symbol}/overrides")
    async def patch_overrides(symbol: str, patch: dict[str, Any]) -> dict[str, Any]:
        _agent(orch, symbol)
        try:
            return (await orch.update_agent_overrides(symbol, patch)).model_dump()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/agents/{symbol}/enabled")
    async def set_enabled(symbol: str, body: EnabledBody) -> dict[str, str]:
        _agent(orch, symbol)
        await orch.set_agent_enabled(symbol, body.enabled)
        return {"ok": "1"}

    @app.post("/api/agents/{symbol}/replan")
    async def replan(symbol: str) -> dict[str, str]:
        _agent(orch, symbol)
        await orch.replan(symbol)
        return {"ok": "1"}

    @app.post("/api/agents/{symbol}/ships/{ship}/role")
    async def set_role(symbol: str, ship: str, body: dict[str, str]) -> dict[str, str]:
        ctx = _agent(orch, symbol)
        pilot = ctx.pilots.get(ship)
        if pilot is None:
            raise HTTPException(404, "no such ship")
        role = body.get("role", "")
        if role == "auto":
            await ctx.set_operator_role(pilot, None)
            await ctx.emit("roles", f"operator released {ship} to automatic roles", ship=ship)
            return {"ok": "1"}
        if role not in ctx.allowed_roles(pilot):
            raise HTTPException(400, f"role must be one of {ctx.allowed_roles(pilot)}")
        await ctx.set_operator_role(pilot, role)
        await ctx.emit("roles", f"operator pinned {ship} → {role}", ship=ship)
        return {"ok": "1"}

    @app.delete("/api/agents/{symbol}")
    async def remove(symbol: str) -> dict[str, str]:
        _agent(orch, symbol)
        await orch.remove_agent(symbol)
        return {"ok": "1"}

    # ------------------------------------------------------------ events
    @app.get("/api/events")
    async def events(
        agent: str | None = None, limit: int = 200, since_id: int = 0
    ) -> list[dict[str, Any]]:
        return await orch.db.list_events(agent, limit=min(limit, 1000), since_id=since_id)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = orch.bus.subscribe()

        async def gen() -> AsyncIterator[str]:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                orch.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------ frontend
    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = FRONTEND_DIST / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


def _agent(orch: Orchestrator, symbol: str) -> Any:
    ctx = orch.agents.get(symbol.upper())
    if ctx is None:
        raise HTTPException(404, "no such agent")
    return ctx


def _schema_fields(model: type[BaseModel]) -> list[dict[str, Any]]:
    schema = model.model_json_schema()
    out = []
    for name, prop in schema["properties"].items():
        typ = prop.get("type")
        enum = prop.get("enum")
        if typ is None and "anyOf" in prop:
            typ = next((a["type"] for a in prop["anyOf"] if a.get("type") != "null"), "string")
            enum = next((a.get("enum") for a in prop["anyOf"] if a.get("enum")), None)
        out.append(
            {
                "name": name,
                "type": typ,
                "description": prop.get("description", ""),
                "default": prop.get("default"),
                "minimum": prop.get("minimum"),
                "maximum": prop.get("maximum"),
                "enum": enum,
            }
        )
    return out


def settings_schema() -> list[dict[str, Any]]:
    return _schema_fields(Settings)


def overrides_schema() -> list[dict[str, Any]]:
    return _schema_fields(AgentOverrides)
