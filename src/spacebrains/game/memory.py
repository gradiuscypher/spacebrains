"""Tactical memory: what each short-horizon choice actually produced.

Every tactical decision that leads somewhere (a role stint, a sell-market pick, a trade route,
a navigation leg) gets an outcome row: how long it took, what it earned, whether it counted as
a success. Two consumers:

- a code-level circuit breaker (`blocked`) removes options whose recent outcomes were failures
  from Jev's candidate set, so a bad pick cannot be repeated indefinitely;
- `summary` is injected into Jev's state and the strategist's game summary so both brains see
  realised results next to the options they are choosing between.
"""

from __future__ import annotations

import time
from typing import Any

from spacebrains.db import Database

Kind = str  # "role" | "sell_market" | "trade_route" | "nav"


class TacticalMemory:
    def __init__(self, db: Database, agent: str) -> None:
        self._db = db
        self._agent = agent

    async def record(
        self,
        kind: Kind,
        key: str,
        *,
        ship: str,
        ok: bool,
        seconds: float,
        credits: int,
        note: str = "",
    ) -> None:
        await self._db.add_outcome(
            self._agent,
            ship=ship,
            kind=kind,
            key=key,
            ok=ok,
            seconds=seconds,
            credits=credits,
            note=note,
        )

    async def blocked(
        self, kind: Kind, *, window_s: float = 2 * 3600, failures: int = 2
    ) -> set[str]:
        """Keys whose last `failures` outcomes (within the window) were all failures."""
        rows = await self._db.list_outcomes(self._agent, kind=kind, since_ts=time.time() - window_s)
        recent: dict[str, list[bool]] = {}
        for r in rows:  # newest first
            recent.setdefault(r["key"], []).append(bool(r["ok"]))
        return {k for k, oks in recent.items() if len(oks) >= failures and not any(oks[:failures])}

    async def stats(self, kind: Kind, *, window_s: float = 6 * 3600) -> dict[str, dict[str, Any]]:
        rows = await self._db.list_outcomes(self._agent, kind=kind, since_ts=time.time() - window_s)
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            st = out.setdefault(
                r["key"], {"n": 0, "ok": 0, "credits": 0, "seconds": 0.0, "last": r["note"] or ""}
            )
            st["n"] += 1
            st["ok"] += int(bool(r["ok"]))
            st["credits"] += int(r["credits"])
            st["seconds"] += float(r["seconds"])
        for st in out.values():
            hours = max(st["seconds"] / 3600, 1 / 60)
            st["credits_per_hour"] = int(st["credits"] / hours)
            st["ok_rate"] = round(st["ok"] / st["n"], 2)
            st["seconds"] = int(st["seconds"])
        return out

    async def ship_history(
        self, *, window_s: float = 6 * 3600, per_ship: int = 3
    ) -> dict[str, list[dict[str, Any]]]:
        rows = await self._db.list_outcomes(
            self._agent, kind="role", since_ts=time.time() - window_s
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            lst = out.setdefault(r["ship"], [])
            if len(lst) < per_ship:
                lst.append(
                    {
                        "role": r["key"],
                        "minutes": int(r["seconds"] / 60),
                        "credits": int(r["credits"]),
                        "ok": bool(r["ok"]),
                        "note": r["note"],
                    }
                )
        return out

    async def summary(self) -> dict[str, Any]:
        """Compact view for the brains: per-kind stats plus currently blocked options."""
        return {
            "roles": await self.stats("role"),
            "sell_markets": await self.stats("sell_market"),
            "trade_routes": await self.stats("trade_route"),
            "nav_failures": await self.stats("nav", window_s=3 * 3600),
            "blocked": {
                "sell_markets": sorted(await self.blocked("sell_market")),
                "trade_routes": sorted(await self.blocked("trade_route")),
                "nav_targets": sorted(await self.blocked("nav", failures=1)),
            },
            "ship_history": await self.ship_history(),
        }
