"""Async SpaceTraders client with a shared token-bucket rate limiter and typed responses.

Rate limits are enforced per IP by the game server (2 req/s sustained, small burst), so a single
`RateLimiter` is shared by every agent's client in the process.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    """Token bucket: `rate` tokens/sec, up to `burst` stored."""

    def __init__(self, rate: float = 2.0, burst: int = 8) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class STClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        limiter: RateLimiter,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._limiter = limiter
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
            await self._limiter.acquire()
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

    async def purchase_ship(self, ship_type: str, waypoint: str) -> dict[str, Any]:
        body = await self._post("/my/ships", {"shipType": ship_type, "waypointSymbol": waypoint})
        return body["data"]

    async def create_chart(self, ship: str) -> dict[str, Any]:
        return (await self._post(f"/my/ships/{ship}/chart"))["data"]


def system_of(waypoint: str) -> str:
    """'X1-ABC-D1' -> 'X1-ABC'."""
    return "-".join(waypoint.split("-")[:2])
