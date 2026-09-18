"""Async SpaceTraders client with a shared token-bucket rate limiter and typed responses.

Rate limits are enforced per IP by the game server (2 req/s sustained, small burst), so a single
`RateLimiter` is shared by every agent's client in the process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx

from spacebrains.st.models import (
    Agent,
    Contract,
    Cooldown,
    Market,
    Ship,
    ShipCargo,
    ShipNav,
    Shipyard,
    Survey,
    System,
    Transaction,
    Waypoint,
)

log = logging.getLogger("spacebrains.st")


class STError(Exception):
    def __init__(self, status: int, code: int, message: str, data: dict[str, Any] | None) -> None:
        super().__init__(f"[{status}/{code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.data = data or {}


class RateLimiter:
    """Token bucket (`rate`/s, `burst` stored) with round-robin fair share between keys.

    The game server limits per IP, so every agent in the process shares this one bucket. Each
    agent acquires under its own key; when tokens are scarce the dispatcher rotates between keys
    so a large fleet cannot starve a small one. Idle keys cost nothing.
    """

    def __init__(self, rate: float = 2.0, burst: int = 8) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._queues: dict[str, deque[asyncio.Future[None]]] = {}
        self._order: deque[str] = deque()
        self._dispatcher: asyncio.Task[None] | None = None
        self._granted: deque[tuple[float, str]] = deque()

    async def acquire(self, key: str = "default") -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if key not in self._queues:
            self._queues[key] = deque()
            self._order.append(key)
        self._queues[key].append(fut)
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.create_task(self._dispatch(), name="ratelimit")
        await fut

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now

    async def _dispatch(self) -> None:
        while any(self._queues.values()):
            self._refill()
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._rate)
                continue
            # Rotate to the next key that has a waiter.
            for _ in range(len(self._order)):
                key = self._order[0]
                self._order.rotate(-1)
                q = self._queues[key]
                while q and q[0].done():  # cancelled waiter
                    q.popleft()
                if q:
                    self._tokens -= 1
                    q.popleft().set_result(None)
                    self._granted.append((time.monotonic(), key))
                    break

    def stats(self, window: float = 60.0) -> dict[str, float]:
        """Requests granted per key in the last `window` seconds."""
        cutoff = time.monotonic() - window
        while self._granted and self._granted[0][0] < cutoff:
            self._granted.popleft()
        out: dict[str, float] = {}
        for _, key in self._granted:
            out[key] = out.get(key, 0) + 1
        return out


class STClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        limiter: RateLimiter,
        http: httpx.AsyncClient | None = None,
        key: str = "account",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._limiter = limiter
        self._key = key
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self.request_count = 0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        for attempt in range(6):
            await self._limiter.acquire(self._key)
            self.request_count += 1
            try:
                resp = await self._http.request(
                    method, self._base + path, json=json, params=params, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                log.warning("transport error on %s %s: %s", method, path, e)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", "1"))
                await asyncio.sleep(min(retry_after, 10) + 0.1)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 204:
                return {}
            body = resp.json()
            if resp.status_code >= 400:
                err = body.get("error", {})
                raise STError(
                    resp.status_code,
                    int(err.get("code", 0)),
                    err.get("message", ""),
                    err.get("data"),
                )
            return body
        raise STError(0, 0, f"gave up on {method} {path}", None)

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("POST", path, json=json)

    async def _paged(self, path: str, limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._get(path, page=page, limit=limit)
            out.extend(body["data"])
            meta = body.get("meta", {})
            if page * limit >= int(meta.get("total", 0)):
                return out
            page += 1

    # -------------------------------------------------------------- account
    async def status(self) -> dict[str, Any]:
        """Server status: reset date, announcements, stats. Unauthenticated."""
        return await self._request("GET", "/", auth=False)

    async def repair_cost(self, ship: str) -> int:
        body = await self._get(f"/my/ships/{ship}/repair")
        return int(body["data"]["transaction"]["totalPrice"])

    async def repair(self, ship: str) -> dict[str, Any]:
        return (await self._post(f"/my/ships/{ship}/repair"))["data"]

    async def register(self, symbol: str, faction: str) -> dict[str, Any]:
        """Uses the account token to create a new agent; returns the raw data incl. agent token."""
        body = await self._post("/register", {"symbol": symbol, "faction": faction})
        return body["data"]

    async def my_agent(self) -> Agent:
        return Agent.model_validate((await self._get("/my/agent"))["data"])

    async def my_ships(self) -> list[Ship]:
        return [Ship.model_validate(s) for s in await self._paged("/my/ships")]

    async def my_ship(self, ship: str) -> Ship:
        return Ship.model_validate((await self._get(f"/my/ships/{ship}"))["data"])

    async def my_contracts(self) -> list[Contract]:
        return [Contract.model_validate(c) for c in await self._paged("/my/contracts")]

    async def accept_contract(self, contract_id: str) -> Contract:
        body = await self._post(f"/my/contracts/{contract_id}/accept")
        return Contract.model_validate(body["data"]["contract"])

    async def deliver_contract(
        self, contract_id: str, ship: str, trade_symbol: str, units: int
    ) -> Contract:
        body = await self._post(
            f"/my/contracts/{contract_id}/deliver",
            {"shipSymbol": ship, "tradeSymbol": trade_symbol, "units": units},
        )
        return Contract.model_validate(body["data"]["contract"])

    async def fulfill_contract(self, contract_id: str) -> Contract:
        body = await self._post(f"/my/contracts/{contract_id}/fulfill")
        return Contract.model_validate(body["data"]["contract"])

    async def negotiate_contract(self, ship: str) -> Contract:
        body = await self._post(f"/my/ships/{ship}/negotiate/contract")
        return Contract.model_validate(body["data"]["contract"])

    # -------------------------------------------------------------- universe
    async def system(self, system: str) -> System:
        return System.model_validate((await self._get(f"/systems/{system}"))["data"])

    async def waypoints(self, system: str) -> list[Waypoint]:
        return [
            Waypoint.model_validate(w) for w in await self._paged(f"/systems/{system}/waypoints")
        ]

    async def market(self, waypoint: str) -> Market:
        system = system_of(waypoint)
        body = await self._get(f"/systems/{system}/waypoints/{waypoint}/market")
        return Market.model_validate(body["data"])

    async def shipyard(self, waypoint: str) -> Shipyard:
        system = system_of(waypoint)
        body = await self._get(f"/systems/{system}/waypoints/{waypoint}/shipyard")
        return Shipyard.model_validate(body["data"])

    # -------------------------------------------------------------- ships
    async def orbit(self, ship: str) -> ShipNav:
        return ShipNav.model_validate((await self._post(f"/my/ships/{ship}/orbit"))["data"]["nav"])

    async def dock(self, ship: str) -> ShipNav:
        return ShipNav.model_validate((await self._post(f"/my/ships/{ship}/dock"))["data"]["nav"])

    async def navigate(self, ship: str, waypoint: str) -> dict[str, Any]:
        body = await self._post(f"/my/ships/{ship}/navigate", {"waypointSymbol": waypoint})
        return body["data"]

    async def set_flight_mode(self, ship: str, mode: str) -> ShipNav:
        body = await self._request("PATCH", f"/my/ships/{ship}/nav", json={"flightMode": mode})
        data = body["data"]
        return ShipNav.model_validate(data.get("nav", data))

    async def refuel(
        self, ship: str, units: int | None = None, from_cargo: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"fromCargo": from_cargo}
        if units is not None:
            payload["units"] = units
        return (await self._post(f"/my/ships/{ship}/refuel", payload))["data"]

    async def extract(self, ship: str, survey: Survey | None = None) -> dict[str, Any]:
        if survey is None:
            return (await self._post(f"/my/ships/{ship}/extract"))["data"]
        body = await self._post(
            f"/my/ships/{ship}/extract/survey", survey.model_dump(by_alias=True, mode="json")
        )
        return body["data"]

    async def siphon(self, ship: str) -> dict[str, Any]:
        return (await self._post(f"/my/ships/{ship}/siphon"))["data"]

    async def survey(self, ship: str) -> tuple[Cooldown, list[Survey]]:
        data = (await self._post(f"/my/ships/{ship}/survey"))["data"]
        return (
            Cooldown.model_validate(data["cooldown"]),
            [Survey.model_validate(s) for s in data["surveys"]],
        )

    async def sell(
        self, ship: str, symbol: str, units: int
    ) -> tuple[ShipCargo, Transaction, Agent]:
        data = (await self._post(f"/my/ships/{ship}/sell", {"symbol": symbol, "units": units}))[
            "data"
        ]
        return (
            ShipCargo.model_validate(data["cargo"]),
            Transaction.model_validate(data["transaction"]),
            Agent.model_validate(data["agent"]),
        )

    async def purchase_cargo(
        self, ship: str, symbol: str, units: int
    ) -> tuple[ShipCargo, Transaction, Agent]:
        data = (await self._post(f"/my/ships/{ship}/purchase", {"symbol": symbol, "units": units}))[
            "data"
        ]
        return (
            ShipCargo.model_validate(data["cargo"]),
            Transaction.model_validate(data["transaction"]),
            Agent.model_validate(data["agent"]),
        )

    async def jettison(self, ship: str, symbol: str, units: int) -> ShipCargo:
        data = (await self._post(f"/my/ships/{ship}/jettison", {"symbol": symbol, "units": units}))[
            "data"
        ]
        return ShipCargo.model_validate(data["cargo"])

    async def transfer(self, ship: str, symbol: str, units: int, to_ship: str) -> ShipCargo:
        """Move cargo from `ship` to `to_ship`; both must share a waypoint and nav status."""
        data = (
            await self._post(
                f"/my/ships/{ship}/transfer",
                {"tradeSymbol": symbol, "units": units, "shipSymbol": to_ship},
            )
        )["data"]
        return ShipCargo.model_validate(data["cargo"])

    async def purchase_ship(self, ship_type: str, waypoint: str) -> dict[str, Any]:
        body = await self._post("/my/ships", {"shipType": ship_type, "waypointSymbol": waypoint})
        return body["data"]

    async def jump_gate(self, waypoint: str) -> list[str]:
        """Gate waypoints (in other systems) connected to this JUMP_GATE waypoint."""
        system = system_of(waypoint)
        body = await self._get(f"/systems/{system}/waypoints/{waypoint}/jump-gate")
        return list(body["data"].get("connections", []))

    async def jump(self, ship: str, gate: str) -> dict[str, Any]:
        """Jump from the gate we orbit to a connected `gate`; buys one antimatter at the market."""
        return (await self._post(f"/my/ships/{ship}/jump", {"waypointSymbol": gate}))["data"]

    async def create_chart(self, ship: str) -> dict[str, Any]:
        return (await self._post(f"/my/ships/{ship}/chart"))["data"]


def system_of(waypoint: str) -> str:
    """'X1-ABC-D1' -> 'X1-ABC'."""
    return "-".join(waypoint.split("-")[:2])
