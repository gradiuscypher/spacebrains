"""Per-ship behaviour loop. One `ShipPilot` task per ship executes the role it is assigned.

Roles: contract | mine | trade | scout | purchase | idle. Each `step()` performs one bounded
action (travel, extract, sell, ...) and returns how long to sleep before the next step.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from spacebrains.game.world import MINABLE_ORES, SIPHONABLE, fuel_cost
from spacebrains.st.client import STError
from spacebrains.st.models import Contract, Ship, Waypoint

if TYPE_CHECKING:
    from spacebrains.game.agent import AgentContext

log = logging.getLogger("spacebrains.pilot")

Role = str
ALL_ROLES: tuple[Role, ...] = ("contract", "mine", "trade", "scout", "haul", "idle")


class ShipPilot:
    def __init__(self, ctx: AgentContext, ship: Ship) -> None:
        self.ctx = ctx
        self.ship = ship
        self.role: Role = "idle"
        self.role_source = "init"
        self.status = "starting"
        self.target: str | None = None
        self.last_error: str | None = None
        # One-off errands the supervisor hands out: ("purchase", ship_type, shipyard) or
        # ("negotiate", "", faction waypoint). Runs before the role step.
        self.errand: tuple[str, str, str] | None = None
        self.in_step = False
        self.needs_refresh = False  # another pilot changed our cargo (transfer)
        self.last_buy_cost = 0
        self._trade_started = 0.0
        self._trade_cost = 0
        self._trade_units = 0
        self._wait_since: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"pilot:{self.ship.symbol}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def set_role(self, role: Role, source: str) -> None:
        if role != self.role:
            self.role = role
            self.role_source = source
            self.target = None
            self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        s = self.ship
        return {
            "symbol": s.symbol,
            "role": self.role,
            "role_source": self.role_source,
            "status": self.status,
            "target": self.target,
            "frame": s.frame.symbol.removeprefix("FRAME_"),
            "ship_role": s.registration.role,
            "waypoint": s.nav.waypoint_symbol,
            "nav_status": s.nav.status,
            "flight_mode": s.nav.flight_mode,
            "arrival_in": round(s.nav.seconds_to_arrival()),
            "fuel": {"current": s.fuel.current, "capacity": s.fuel.capacity},
            "cargo": {
                "units": s.cargo.units,
                "capacity": s.cargo.capacity,
                "inventory": {i.symbol: i.units for i in s.cargo.inventory},
            },
            "cooldown": round(s.cooldown_remaining),
            "can_mine": s.can_mine,
            "can_siphon": s.can_siphon,
            "speed": s.engine.speed,
            "last_error": self.last_error,
        }

    async def _run(self) -> None:
        await asyncio.sleep(1)
        while True:
            if self.ctx.paused:
                self.status = "paused"
                await self._sleep(5)
                continue
            self.in_step = True
            try:
                delay = await self.step()
            except STError as e:
                self.last_error = e.message
                self.status = f"error: {e.message[:80]}"
                await self.ctx.emit(
                    "error", f"{self.ship.symbol}: {e.message}", ship=self.ship.symbol
                )
                delay = 15.0
                if e.code in (4214, 4000):  # in transit / cooldown: refresh ship and wait it out
                    await self.refresh()
                    delay = max(self.ship.nav.seconds_to_arrival(), self.ship.cooldown_remaining, 5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("pilot %s crashed in step", self.ship.symbol)
                self.last_error = str(e)
                self.status = f"crash: {str(e)[:80]}"
                delay = 20.0
            finally:
                self.in_step = False
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.5, seconds))

    # ------------------------------------------------------------ helpers
    @property
    def wp(self) -> str:
        return self.ship.nav.waypoint_symbol

    @property
    def system(self) -> str:
        return self.ship.nav.system_symbol

    async def refresh(self) -> None:
        self.ship = await self.ctx.client.my_ship(self.ship.symbol)

    async def wait_arrival(self) -> float:
        return self.ship.nav.seconds_to_arrival() + 1

    async def ensure_orbit(self) -> None:
        if self.ship.nav.status == "DOCKED":
            self.ship.nav = await self.ctx.client.orbit(self.ship.symbol)

    async def ensure_docked(self) -> None:
        if self.ship.nav.status == "IN_ORBIT":
            self.ship.nav = await self.ctx.client.dock(self.ship.symbol)
            await self.observe_here()

    async def observe_here(self) -> None:
        """Record market/shipyard data for the waypoint we are docked at."""
        sw = await self.ctx.system_world()
        w = sw.waypoints.get(self.wp)
        if w is None:
            return
        if w.is_market:
            market = await self.ctx.client.market(self.wp)
            await self.ctx.world.record_market(market)
        if w.is_shipyard:
            yard = await self.ctx.client.shipyard(self.wp)
            await self.ctx.world.record_shipyard(self.system, yard)

    async def refuel_if_possible(self, threshold: float = 0.6) -> None:
        s = self.ship
        if s.fuel.capacity == 0 or s.fuel.current >= s.fuel.capacity * threshold:
            return
        sw = await self.ctx.system_world()
        w = sw.waypoints.get(self.wp)
        if w is None or not w.is_market:
            return
        await self.ensure_docked()
        try:
            data = await self.ctx.client.refuel(s.symbol)
        except STError as e:
            if e.code == 4600:  # market doesn't sell fuel
                return
            raise
        s.fuel.current = data["fuel"]["current"]
        tx = data.get("transaction", {})
        await self.ctx.on_credits(data["agent"]["credits"])
        await self.ctx.emit(
            "refuel",
            f"{s.symbol} refuelled {tx.get('units', '?')} units for {tx.get('totalPrice', '?')}c",
            ship=s.symbol,
        )

    async def go_to(self, waypoint: str) -> float | None:
        """Navigate toward `waypoint`. Returns seconds until arrival, or None if already there."""
        if self.wp == waypoint and self.ship.nav.status != "IN_TRANSIT":
            return None
        if self.ship.nav.status == "IN_TRANSIT":
            return await self.wait_arrival()
        sw = await self.ctx.system_world()
        dist = sw.dist(self.wp, waypoint)
        s = self.ship
        mode = "CRUISE"
        if s.fuel.capacity > 0:
            need = fuel_cost(dist, "CRUISE")
            if s.fuel.current < need:
                await self.refuel_if_possible(threshold=1.0)
            if s.fuel.current < need:
                mode = "DRIFT"
        if s.nav.flight_mode != mode:
            s.nav = await self.ctx.client.set_flight_mode(s.symbol, mode)
        await self.ensure_orbit()
        data = await self.ctx.client.navigate(s.symbol, waypoint)
        s.nav = s.nav.model_validate(data["nav"])
        s.fuel.current = data["fuel"]["current"]
        eta = s.nav.seconds_to_arrival()
        self.status = f"→ {waypoint} ({mode.lower()}, {int(eta)}s)"
        await self.ctx.emit(
            "navigate",
            f"{s.symbol} → {waypoint} in {mode} ({int(eta)}s)",
            ship=s.symbol,
            data={"to": waypoint, "mode": mode, "eta": eta},
        )
        return eta + 1

    async def sell_cargo(self, keep: set[str] | None = None) -> int:
        """Sell everything the local market takes. Returns credits earned."""
        keep = keep or set()
        await self.ensure_docked()
        market = await self.ctx.client.market(self.wp)
        await self.ctx.world.record_market(market)
        tradeable = {g.symbol: g for g in market.trade_goods or []}
        earned = 0
        for item in list(self.ship.cargo.inventory):
            if item.symbol in keep or item.symbol not in tradeable:
                continue
            vol = max(1, tradeable[item.symbol].trade_volume)
            remaining = item.units
            while remaining > 0:
                units = min(vol, remaining)
                cargo, tx, agent = await self.ctx.client.sell(self.ship.symbol, item.symbol, units)
                self.ship.cargo = cargo
                remaining -= units
                earned += tx.total_price
                await self.ctx.on_credits(agent.credits)
            await self.ctx.emit(
                "sell",
                f"{self.ship.symbol} sold {item.units} {item.symbol} at {self.wp}",
                ship=self.ship.symbol,
                data={"good": item.symbol, "units": item.units, "waypoint": self.wp},
            )
        return earned

    async def jettison_unsellable(self, keep: set[str]) -> None:
        for item in list(self.ship.cargo.inventory):
            if item.symbol in keep:
                continue
            self.ship.cargo = await self.ctx.client.jettison(
                self.ship.symbol, item.symbol, item.units
            )
            await self.ctx.emit(
                "jettison",
                f"{self.ship.symbol} jettisoned {item.units} {item.symbol}",
                ship=self.ship.symbol,
            )

    # ------------------------------------------------------------ step
    async def step(self) -> float:
        if self.ship.nav.status == "IN_TRANSIT":
            eta = self.ship.nav.seconds_to_arrival()
            if eta > 0:
                self.status = f"in transit → {self.ship.nav.route.destination.symbol} ({int(eta)}s)"
                return eta + 1
            await self.refresh()
            await self.refuel_if_possible()
        if self.needs_refresh:
            self.needs_refresh = False
            await self.refresh()
        if self.errand:
            return await self.step_errand()
        handler = {
            "contract": self.step_contract,
            "mine": self.step_mine,
            "trade": self.step_trade,
            "scout": self.step_scout,
            "haul": self.step_haul,
            "idle": self.step_idle,
        }.get(self.role, self.step_idle)
        return await handler()

    async def step_idle(self) -> float:
        self.status = "idle"
        return 30

    # ---------------------------------------------------------------- scout
    async def step_scout(self) -> float:
        sw = await self.ctx.system_world()
        stale = await self.ctx.world.market_staleness(self.system)
        if not stale:
            self.status = "no markets to scout"
            return 60
        # Visit the stalest market, tie-break by distance; also record shipyards en route.
        here = self.wp
        target = self.target
        if target is None or stale.get(target, 0) < 600:
            target = max(stale, key=lambda w: (min(stale[w], 86400 * 30), -sw.dist(here, w)))
            if stale[target] < 600:
                self.status = "all markets fresh"
                return 120
            self.target = target
        eta = await self.go_to(target)
        if eta is not None:
            return eta
        await self.ensure_docked()
        await self.observe_here()
        await self.refuel_if_possible()
        await self.ctx.emit(
            "scout", f"{self.ship.symbol} observed market {target}", ship=self.ship.symbol
        )
        self.target = None
        return 1

    # ---------------------------------------------------------------- mine
    def _mining_target(self, sw: Any) -> Waypoint | None:
        cands = (
            sw.asteroids()
            if self.ship.can_mine
            else sw.gas_giants()
            if self.ship.can_siphon
            else []
        )
        if not cands:
            return None
        engineered = [w for w in cands if w.type == "ENGINEERED_ASTEROID"]
        return sw.nearest(self.wp, engineered or cands)

    async def step_mine(self, keep_goods: set[str] | None = None) -> float:
        sw = await self.ctx.system_world()
        s = self.ship
        if not (s.can_mine or s.can_siphon):
            self.set_role("scout", "no mining mount")
            return 1
        if s.cargo.units >= s.cargo.capacity * 0.9 or (s.cargo.free < 3 and s.cargo.units > 0):
            return await self._unload(keep_goods or set())
        target = self._mining_target(sw)
        if target is None:
            self.status = "no asteroid in system"
            return 120
        eta = await self.go_to(target.symbol)
        if eta is not None:
            return eta
        if s.cooldown_remaining > 0:
            self.status = f"cooldown {int(s.cooldown_remaining)}s at {self.wp}"
            return s.cooldown_remaining + 0.5
        await self.ensure_orbit()
        if s.can_siphon and not s.can_mine:
            data = await self.ctx.client.siphon(s.symbol)
        else:
            # Surveys multiply yields and let us target the contract good.
            surveys = self.ctx.usable_surveys(self.wp)
            if s.can_survey and not surveys:
                cooldown, found = await self.ctx.client.survey(s.symbol)
                s.cooldown = cooldown
                self.ctx.add_surveys(found)
                self.status = f"surveyed {self.wp}: {len(found)} deposits"
                await self.ctx.emit(
                    "survey",
                    f"{s.symbol} surveyed {self.wp}: "
                    + ", ".join(
                        f"{x.size} [{' '.join(d.get('symbol', '') for d in x.deposits)}]"
                        for x in found
                    ),
                    ship=s.symbol,
                )
                return s.cooldown_remaining + 0.5
            survey = self.ctx.best_survey(surveys, keep_goods or set())
            try:
                data = await self.ctx.client.extract(s.symbol, survey)
            except STError as e:
                if survey is not None and e.code in (4221, 4224):  # expired / exhausted
                    self.ctx.drop_survey(survey)
                    return 1
                raise
        s.cargo = s.cargo.model_validate(data["cargo"])
        s.cooldown = s.cooldown.model_validate(data["cooldown"])
        got = data.get("extraction", data.get("siphon", {})).get("yield", {})
        self.status = f"mining at {self.wp} ({s.cargo.units}/{s.cargo.capacity})"
        await self.ctx.emit(
            "extract",
            f"{s.symbol} extracted {got.get('units')} {got.get('symbol')} ({s.cargo.units}/{s.cargo.capacity})",
            ship=s.symbol,
            data=got,
            persist=False,
        )
        return s.cooldown_remaining + 0.5

    async def _unload(self, keep: set[str]) -> float:
        """Hand cargo to a hauler parked here if there is one; otherwise go sell it ourselves.

        `keep` goods are contract goods: a hauler takes those too (it delivers them), but a
        solo sell trip keeps them aboard.
        """
        hauler = self.ctx.hauler_at(self.wp)
        if hauler is not None and hauler.ship.cargo.free > 0:
            moved = await self._transfer_to(hauler)
            if moved:
                self._wait_since = None
                return 1
        inbound = self.ctx.hauler_inbound(self.wp)
        if inbound is not None:
            # A shuttle is on its way: wait a bounded time rather than leave the asteroid.
            if self._wait_since is None:
                self._wait_since = time.time()
            if time.time() - self._wait_since < 240:
                self.status = f"full, waiting for hauler {inbound.ship.symbol}"
                return 20
        self._wait_since = None
        return await self._sell_trip(keep)

    async def _transfer_to(self, hauler: ShipPilot) -> int:
        await self.ensure_orbit()
        moved = 0
        for item in list(self.ship.cargo.inventory):
            units = min(item.units, hauler.ship.cargo.free)
            if units <= 0:
                break
            try:
                self.ship.cargo = await self.ctx.client.transfer(
                    self.ship.symbol, item.symbol, units, hauler.ship.symbol
                )
            except STError as e:
                if e.code in (4217, 4218, 4219, 4234):  # hauler full / not here / status mismatch
                    hauler.needs_refresh = True
                    break
                raise
            hauler.ship.cargo.units += units
            existing = next(
                (i for i in hauler.ship.cargo.inventory if i.symbol == item.symbol), None
            )
            if existing:
                existing.units += units
            else:
                hauler.ship.cargo.inventory.append(item.model_copy(update={"units": units}))
            hauler.needs_refresh = True
            moved += units
        if moved:
            self.status = f"handed {moved} units to {hauler.ship.symbol}"
            await self.ctx.emit(
                "transfer",
                f"{self.ship.symbol} transferred {moved} units to {hauler.ship.symbol} at {self.wp}",
                ship=self.ship.symbol,
                data={"to": hauler.ship.symbol, "units": moved},
                persist=False,
            )
        return moved

    async def _sell_trip(self, keep: set[str]) -> float:
        sw = await self.ctx.system_world()
        goods = [i.symbol for i in self.ship.cargo.inventory if i.symbol not in keep]
        if not goods:
            return 5
        if self.target is None:
            options = await self.ctx.world.best_sell_markets(self.system, goods, self.wp)
            choice: str | None = None
            if len(options) > 1 and self.ctx.jev_enabled:
                picked = await self.ctx.jev.choose_option(
                    agent=self.ctx.symbol,
                    purpose="pick_sell_market",
                    instructions=(
                        "Which market should this mining ship sell its cargo at? Prefer higher "
                        "total revenue and coverage of more cargo goods; penalise long trips."
                    ),
                    situation={
                        "cargo": {i.symbol: i.units for i in self.ship.cargo.inventory},
                        "fuel": self.ship.fuel.current,
                        "options": options,
                    },
                    options={
                        o[
                            "waypoint"
                        ]: f"sells {o['covers']} of our goods, {o['distance']} units away, prices {o['prices']}"
                        for o in options
                    },
                )
                if picked:
                    choice = picked[0]
            if choice is None and options:
                choice = options[0]["waypoint"]
            if choice is None:
                near = sw.nearest(self.wp, sw.markets())
                choice = near.symbol if near else None
            if choice is None:
                self.status = "nowhere to sell"
                return 120
            self.target = choice
        eta = await self.go_to(self.target)
        if eta is not None:
            return eta
        earned = await self.sell_cargo(keep=keep)
        await self.refuel_if_possible()
        # Anything the market wouldn't take and no other known market buys: dump it.
        leftovers = [i.symbol for i in self.ship.cargo.inventory if i.symbol not in keep]
        if leftovers:
            others = await self.ctx.world.best_sell_markets(self.system, leftovers, self.wp)
            if not others:
                await self.jettison_unsellable(keep)
            else:
                self.target = others[0]["waypoint"]
                return 1
        self.status = f"sold cargo at {self.wp} for {earned}c"
        self.target = None
        return 1

    # ---------------------------------------------------------------- contract
    async def step_contract(self) -> float:
        contract = self.ctx.active_contract()
        if contract is None:
            self.set_role("mine" if self.ship.can_mine else "scout", "no active contract")
            return 1
        deliverable = next((d for d in contract.terms.deliver if d.remaining > 0), None)
        if deliverable is None:
            return await self._fulfill(contract)
        good, dest = deliverable.trade_symbol, deliverable.destination_symbol
        have = self.ship.cargo.units_of(good)
        s = self.ship

        # Deliver when we can finish the contract or the hold is mostly contract goods;
        # if the hold is full of by-catch instead, sell that first and keep mining.
        full = s.cargo.free < 3
        hauler = self.ctx.hauler_at(self.wp)
        can_hand_off = full and hauler is not None and hauler.ship.cargo.free > 0
        if (
            can_hand_off
            and hauler is not None
            and self.wp != dest
            and await self._transfer_to(hauler)
        ):
            return 1
        deliver_now = have >= deliverable.remaining or self.wp == dest
        if have > 0 and (deliver_now or (full and have >= s.cargo.units * 0.5)):
            eta = await self.go_to(dest)
            if eta is not None:
                return eta
            await self.ensure_docked()
            units = min(have, deliverable.remaining)
            updated = await self.ctx.client.deliver_contract(contract.id, s.symbol, good, units)
            self.ctx.update_contract(updated)
            await self.refresh()
            sw = await self.ctx.system_world()
            if s.cargo.units > 0 and sw.waypoints[self.wp].is_market:
                await self.sell_cargo(keep={good})
            await self.refuel_if_possible()
            await self.ctx.emit(
                "deliver",
                f"{s.symbol} delivered {units} {good} to {dest}",
                ship=s.symbol,
                data={"good": good, "units": units, "contract": contract.id},
            )
            if all(d.remaining <= 0 for d in updated.terms.deliver):
                return await self._fulfill(updated)
            return 1

        can_mine_it = (good in MINABLE_ORES and s.can_mine) or (good in SIPHONABLE and s.can_siphon)
        if can_mine_it:
            # Sell by-catch before it clogs the hold, then keep mining.
            if full and have < s.cargo.units:
                return await self._unload(keep={good})
            return await self.step_mine(keep_goods={good})

        source = await self.ctx.world.cheapest_source(self.system, good)
        if source is None or s.cargo.capacity == 0:
            self.status = f"no known source for {good}"
            self.set_role("mine" if s.can_mine else "scout", f"cannot source {good}")
            return 1
        need = min(deliverable.remaining - have, s.cargo.free)
        affordable = (self.ctx.credits - self.ctx.settings.min_credit_reserve) // max(
            source["price"], 1
        )
        units = min(need, affordable)
        if units <= 0:
            self.status = f"cannot afford {good} ({source['price']}c)"
            return 60
        eta = await self.go_to(source["waypoint"])
        if eta is not None:
            return eta
        await self.ensure_docked()
        bought = await self._buy(good, units, source.get("volume") or units)
        await self.ctx.emit(
            "buy", f"{s.symbol} bought {bought} {good} at {self.wp} for contract", ship=s.symbol
        )
        return 1

    async def _fulfill(self, contract: Contract) -> float:
        updated = await self.ctx.client.fulfill_contract(contract.id)
        self.ctx.update_contract(updated)
        await self.ctx.refresh_agent()
        await self.ctx.emit(
            "contract_fulfilled",
            f"Contract {contract.id} fulfilled (+{contract.terms.payment.on_fulfilled}c)",
            ship=self.ship.symbol,
            data={"contract": contract.id, "payout": contract.terms.payment.on_fulfilled},
        )
        self.ctx.request_replan("contract fulfilled")
        # Close the loop right here if the drop-off point has a faction presence.
        sw = await self.ctx.system_world()
        w = sw.waypoints.get(self.wp)
        if w is not None and w.faction is not None and self.ship.nav.status == "DOCKED":
            await self.ctx.try_negotiate(self)
        return 1

    async def _buy(self, good: str, units: int, volume: int) -> int:
        """Buy in trade-volume chunks; sets `last_buy_cost` for ledger bookkeeping."""
        bought = 0
        self.last_buy_cost = 0
        while units > 0:
            chunk = min(max(1, volume), units)
            cargo, tx, agent = await self.ctx.client.purchase_cargo(self.ship.symbol, good, chunk)
            self.ship.cargo = cargo
            await self.ctx.on_credits(agent.credits)
            bought += chunk
            self.last_buy_cost += tx.total_price
            units -= chunk
        return bought

    # ---------------------------------------------------------------- trade
    async def step_trade(self) -> float:
        s = self.ship
        if s.cargo.capacity == 0:
            self.set_role("scout", "no cargo hold")
            return 1
        route = self.ctx.trade_plans.get(s.symbol)
        if route is None:
            routes = await self.ctx.world.best_routes(self.system, s.cargo.capacity)
            routes = [r for r in routes if r.margin * min(s.cargo.capacity, r.volume) > 1500]
            if not routes:
                self.status = "no profitable route known"
                self.set_role("mine" if s.can_mine else "scout", "no trade route")
                return 1
            chosen = routes[0]
            if len(routes) > 1 and self.ctx.jev_enabled:
                picked = await self.ctx.jev.choose_option(
                    agent=self.ctx.symbol,
                    purpose="pick_trade_route",
                    instructions=(
                        "Pick the trade route for this hauler. Favour high total profit and short "
                        "distance; be wary of tiny trade volumes that cap how much we can move."
                    ),
                    situation={
                        "cargo_capacity": s.cargo.capacity,
                        "credits": self.ctx.credits,
                        "routes": [r.as_dict() for r in routes],
                    },
                    options={f"{r.good}@{r.buy_at}": str(r.as_dict()) for r in routes},
                )
                if picked:
                    chosen = next(
                        (r for r in routes if f"{r.good}@{r.buy_at}" == picked[0]), chosen
                    )
            self.ctx.trade_plans[s.symbol] = chosen
            route = chosen
            await self.ctx.emit(
                "trade_plan",
                f"{s.symbol} trading {route.good}: {route.buy_at} ({route.buy_price}) → "
                f"{route.sell_at} ({route.sell_price})",
                ship=s.symbol,
                data=route.as_dict(),
            )
        holding = s.cargo.units_of(route.good)
        if holding == 0:
            eta = await self.go_to(route.buy_at)
            if eta is not None:
                return eta
            await self.ensure_docked()
            await self.sell_cargo()
            market = await self.ctx.client.market(self.wp)
            await self.ctx.world.record_market(market)
            good = next((g for g in market.trade_goods or [] if g.symbol == route.good), None)
            if good is None or good.purchase_price >= route.sell_price:
                await self.ctx.emit(
                    "trade_abort", f"{s.symbol}: spread on {route.good} gone", ship=s.symbol
                )
                self.ctx.trade_plans.pop(s.symbol, None)
                return 1
            budget = self.ctx.credits - self.ctx.settings.min_credit_reserve
            units = min(s.cargo.free, budget // good.purchase_price)
            if units <= 0:
                self.status = "no credits for trade"
                self.ctx.trade_plans.pop(s.symbol, None)
                return 60
            bought = await self._buy(route.good, units, good.trade_volume)
            self._trade_started = time.time()
            self._trade_cost = self.last_buy_cost
            self._trade_units = bought
            await self.ctx.emit(
                "buy",
                f"{s.symbol} bought {bought} {route.good} at {self.wp} for {self.last_buy_cost}c",
                ship=s.symbol,
            )
            await self.refuel_if_possible()
            return 1
        eta = await self.go_to(route.sell_at)
        if eta is not None:
            return eta
        earned = await self.sell_cargo()
        await self.refuel_if_possible()
        self.ctx.trade_plans.pop(s.symbol, None)
        profit = earned - self._trade_cost
        await self.ctx.db.add_trade(
            self.ctx.symbol,
            ship=s.symbol,
            good=route.good,
            buy_at=route.buy_at,
            sell_at=route.sell_at,
            units=self._trade_units,
            cost=self._trade_cost,
            revenue=earned,
            predicted_margin=route.margin * self._trade_units,
            seconds=time.time() - self._trade_started,
        )
        await self.ctx.emit(
            "trade_done",
            f"{s.symbol} {route.good} {route.buy_at}→{route.sell_at}: {profit:+}c realised "
            f"(predicted {route.margin * self._trade_units:+}c)",
            ship=s.symbol,
            data={"profit": profit, "predicted": route.margin * self._trade_units},
        )
        self.status = f"trade done: {profit:+}c"
        return 1

    # ---------------------------------------------------------------- haul
    async def step_haul(self) -> float:
        s = self.ship
        if s.cargo.capacity == 0:
            self.set_role("scout", "no cargo hold")
            return 1
        hub = self.ctx.mining_hub()
        if hub is None:
            self.set_role("trade", "no miners to shuttle for")
            return 1
        contract = self.ctx.active_contract()
        keep: set[str] = set()
        contract_good: str | None = None
        dest: str | None = None
        remaining = 0
        if contract is not None:
            d = next((d for d in contract.terms.deliver if d.remaining > 0), None)
            if d is not None:
                contract_good, dest, remaining = d.trade_symbol, d.destination_symbol, d.remaining
                keep = {contract_good}
        have = s.cargo.units_of(contract_good) if contract_good else 0

        # Dispose when full, or when we've been holding a partial load with nothing coming.
        idle_miners = not self.ctx.miners_with_cargo(hub)
        holding_long = (
            s.cargo.units > 0
            and self._wait_since is not None
            and time.time() - self._wait_since > 300
            and idle_miners
        )
        if s.cargo.free < 3 or holding_long or (contract_good and have >= remaining > 0):
            self._wait_since = None
            if (
                contract is not None
                and contract_good
                and dest
                and (have >= remaining or have >= s.cargo.capacity * 0.4)
            ):
                eta = await self.go_to(dest)
                if eta is not None:
                    return eta
                await self.ensure_docked()
                units = min(have, remaining)
                updated = await self.ctx.client.deliver_contract(
                    contract.id, s.symbol, contract_good, units
                )
                self.ctx.update_contract(updated)
                await self.refresh()
                sw = await self.ctx.system_world()
                if s.cargo.units > 0 and sw.waypoints[self.wp].is_market:
                    await self.sell_cargo(keep=keep)
                await self.refuel_if_possible()
                await self.ctx.emit(
                    "deliver",
                    f"{s.symbol} (hauler) delivered {units} {contract_good} to {dest}",
                    ship=s.symbol,
                    data={"good": contract_good, "units": units},
                )
                if all(d.remaining <= 0 for d in updated.terms.deliver):
                    return await self._fulfill(updated)
                return 1
            if any(i.symbol not in keep for i in s.cargo.inventory):
                return await self._sell_trip(keep)
        eta = await self.go_to(hub)
        if eta is not None:
            self.status = f"hauler → {hub}"
            return eta
        await self.ensure_orbit()
        if self._wait_since is None:
            self._wait_since = time.time()
        self.status = f"collecting at {hub} ({s.cargo.units}/{s.cargo.capacity})"
        return 20

    # ---------------------------------------------------------------- purchase
    async def step_errand(self) -> float:
        assert self.errand is not None
        kind, arg, where = self.errand
        eta = await self.go_to(where)
        if eta is not None:
            self.status = f"errand: {kind} {arg} at {where}"
            return eta
        await self.ensure_docked()
        await self.observe_here()
        self.errand = None
        if kind == "purchase":
            await self.ctx.try_purchase_ship(arg, where)
        elif kind == "negotiate":
            await self.ctx.try_negotiate(self)
        return 1


def now() -> float:
    return time.time()
