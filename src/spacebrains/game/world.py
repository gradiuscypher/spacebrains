"""Per-system knowledge: waypoints, market observations, shipyard listings, trade math."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from spacebrains.db import Database
from spacebrains.st.client import STClient
from spacebrains.st.models import Market, Shipyard, Waypoint

MINABLE_ORES = {
    "IRON_ORE",
    "COPPER_ORE",
    "ALUMINUM_ORE",
    "SILVER_ORE",
    "GOLD_ORE",
    "PLATINUM_ORE",
    "URANITE_ORE",
    "MERITIUM_ORE",
    "QUARTZ_SAND",
    "SILICON_CRYSTALS",
    "AMMONIA_ICE",
    "ICE_WATER",
    "PRECIOUS_STONES",
    "DIAMONDS",
}
SIPHONABLE = {"HYDROCARBON", "LIQUID_HYDROGEN", "LIQUID_NITROGEN"}
MARKET_FRESH_SECONDS = 3 * 3600


def distance(a: Waypoint, b: Waypoint) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def travel_seconds(dist: float, speed: int, mode: str = "CRUISE") -> float:
    mult = {"CRUISE": 25, "DRIFT": 250, "BURN": 12.5, "STEALTH": 30}[mode]
    return round(max(1.0, dist) * mult / max(speed, 1) + 15)


def fuel_cost(dist: float, mode: str = "CRUISE") -> int:
    if mode == "DRIFT":
        return 1
    d = max(1, round(dist))
    return {"CRUISE": d, "BURN": 2 * d, "STEALTH": d}[mode]


@dataclass(slots=True)
class TradeRoute:
    good: str
    buy_at: str
    sell_at: str
    buy_price: int
    sell_price: int
    volume: int
    distance: float

    @property
    def margin(self) -> int:
        return self.sell_price - self.buy_price

    def as_dict(self) -> dict[str, Any]:
        return {
            "good": self.good,
            "buy_at": self.buy_at,
            "sell_at": self.sell_at,
            "buy_price": self.buy_price,
            "sell_price": self.sell_price,
            "margin_per_unit": self.margin,
            "volume": self.volume,
            "distance": round(self.distance),
        }


@dataclass(slots=True)
class SystemWorld:
    symbol: str
    waypoints: dict[str, Waypoint] = field(default_factory=dict)
    shipyards: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # wp -> listings
    loaded_at: float = 0.0

    def wp(self, symbol: str) -> Waypoint:
        return self.waypoints[symbol]

    def markets(self) -> list[Waypoint]:
        return [w for w in self.waypoints.values() if w.is_market]

    def shipyard_waypoints(self) -> list[Waypoint]:
        return [w for w in self.waypoints.values() if w.is_shipyard]

    def asteroids(self) -> list[Waypoint]:
        return [w for w in self.waypoints.values() if w.is_asteroid]

    def gas_giants(self) -> list[Waypoint]:
        return [w for w in self.waypoints.values() if w.type == "GAS_GIANT"]

    def nearest(self, origin: str, candidates: list[Waypoint]) -> Waypoint | None:
        if not candidates:
            return None
        o = self.wp(origin)
        return min(candidates, key=lambda w: distance(o, w))

    def dist(self, a: str, b: str) -> float:
        return distance(self.wp(a), self.wp(b))


class World:
    """Caches systems and wraps the DB market tables with trade helpers."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.systems: dict[str, SystemWorld] = {}

    async def load_system(self, client: STClient, system: str) -> SystemWorld:
        sw = self.systems.get(system)
        if sw and time.time() - sw.loaded_at < 6 * 3600:
            return sw
        wps = await client.waypoints(system)
        sw = SystemWorld(symbol=system, waypoints={w.symbol: w for w in wps}, loaded_at=time.time())
        cached = await self._db.get_kv(f"shipyards:{system}")
        if cached:
            sw.shipyards = cached
        self.systems[system] = sw
        return sw

    async def record_market(self, market: Market) -> None:
        if market.trade_goods:
            await self._db.upsert_market(
                market.symbol, [g.model_dump(by_alias=True) for g in market.trade_goods]
            )

    async def record_shipyard(self, system: str, yard: Shipyard) -> None:
        sw = self.systems[system]
        if yard.ships:
            sw.shipyards[yard.symbol] = [
                {"type": s.type, "price": s.purchase_price, "supply": s.supply, "seen": time.time()}
                for s in yard.ships
            ]
        else:
            sw.shipyards.setdefault(
                yard.symbol,
                [{"type": t.type, "price": None, "seen": time.time()} for t in yard.ship_types],
            )
        await self._db.set_kv(f"shipyards:{system}", sw.shipyards)

    async def market_staleness(self, system: str) -> dict[str, float]:
        """Seconds since each marketplace was last observed (inf if never)."""
        sw = self.systems[system]
        seen = await self._db.market_observed_at(system)
        now = time.time()
        return {w.symbol: now - seen.get(w.symbol, -math.inf) for w in sw.markets()}

    async def fresh_rows(self, system: str) -> list[dict[str, Any]]:
        cutoff = time.time() - MARKET_FRESH_SECONDS
        return [r for r in await self._db.market_rows(system) if r["observed_at"] >= cutoff]

    async def best_sell_markets(
        self, system: str, goods: list[str], origin: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Markets buying any of `goods`, ranked by coverage, unit revenue, then distance."""
        rows = await self.fresh_rows(system)
        sw = self.systems[system]
        by_wp: dict[str, dict[str, int]] = {}
        for r in rows:
            if r["good"] in goods and r["sell"]:
                by_wp.setdefault(r["waypoint"], {})[r["good"]] = int(r["sell"])
        scored = [
            (len(prices), sum(prices.values()), round(sw.dist(origin, wp)), wp, prices)
            for wp, prices in by_wp.items()
            if wp in sw.waypoints
        ]
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        return [
            {"waypoint": wp, "prices": prices, "distance": dist, "covers": covers}
            for covers, _rev, dist, wp, prices in scored[:limit]
        ]

    async def cheapest_source(self, system: str, good: str) -> dict[str, Any] | None:
        rows = [r for r in await self.fresh_rows(system) if r["good"] == good and r["buy"]]
        if not rows:
            return None
        r = min(rows, key=lambda r: r["buy"])
        return {"waypoint": r["waypoint"], "price": int(r["buy"]), "volume": int(r["volume"] or 0)}

    async def best_routes(self, system: str, capacity: int, limit: int = 5) -> list[TradeRoute]:
        rows = await self.fresh_rows(system)
        sw = self.systems[system]
        buys: dict[str, list[dict[str, Any]]] = {}
        sells: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["waypoint"] not in sw.waypoints:
                continue
            if r["kind"] in ("export", "exchange") and r["buy"]:
                buys.setdefault(r["good"], []).append(r)
            if r["kind"] in ("import", "exchange") and r["sell"]:
                sells.setdefault(r["good"], []).append(r)
        routes: list[TradeRoute] = []
        for good, bs in buys.items():
            for b in bs:
                for s in sells.get(good, []):
                    if s["waypoint"] == b["waypoint"]:
                        continue
                    margin = int(s["sell"]) - int(b["buy"])
                    if margin <= 0:
                        continue
                    routes.append(
                        TradeRoute(
                            good=good,
                            buy_at=b["waypoint"],
                            sell_at=s["waypoint"],
                            buy_price=int(b["buy"]),
                            sell_price=int(s["sell"]),
                            volume=min(int(b["volume"] or capacity), int(s["volume"] or capacity)),
                            distance=sw.dist(b["waypoint"], s["waypoint"]),
                        )
                    )
        routes.sort(key=lambda r: -(r.margin * min(capacity, r.volume) - r.distance * 2))
        return routes[:limit]
