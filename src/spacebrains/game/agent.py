"""One `AgentContext` per SpaceTraders agent: owns its ships' pilots, contracts, goals and plan.

The supervisor loop (every `tick_seconds`) refreshes state, syncs pilots with the fleet, accepts
contracts, assigns roles (Jev), executes fleet-growth from the plan, and triggers the
strategist (OpenRouter) on its interval or on notable events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from spacebrains.brain.jev import JevBrain
from spacebrains.brain.openrouter import BudgetExceededError
from spacebrains.brain.strategist import Plan, Strategist
from spacebrains.db import AgentRow, Database
from spacebrains.events import EventBus
from spacebrains.game.memory import TacticalMemory
from spacebrains.game.pilot import ShipPilot
from spacebrains.game.world import MINABLE_ORES, SIPHONABLE, SystemWorld, TradeRoute, World
from spacebrains.settings import AgentOverrides, Settings, effective
from spacebrains.st.client import STClient, STError
from spacebrains.st.models import Agent, Contract, Survey

log = logging.getLogger("spacebrains.agent")


class AgentContext:
    def __init__(
        self,
        row: AgentRow,
        *,
        client: STClient,
        db: Database,
        bus: EventBus,
        world: World,
        jev: JevBrain,
        strategist: Strategist,
        global_settings: Settings,
    ) -> None:
        self.symbol = row.symbol
        self.row = row
        self.client = client
        self.db = db
        self.bus = bus
        self.world = world
        self.jev = jev
        self.strategist = strategist
        self._global = global_settings
        self.overrides = AgentOverrides.model_validate(row.overrides)
        self.memory = TacticalMemory(db, row.symbol)

        self.agent: Agent | None = None
        self.credits = 0
        self.contracts: dict[str, Contract] = {}
        self.pilots: dict[str, ShipPilot] = {}
        self.trade_plans: dict[str, TradeRoute] = {}
        self.plan: Plan | None = None
        self.plan_ts = 0.0
        self.plan_error: str | None = None
        self.last_roles_ts = 0.0
        self.replan_reason: str | None = None
        self.purchase_pending = False
        self._contracts_checked_ts = 0.0
        self._negotiate_ts = 0.0
        self._known_export_goods: set[str] = set()
        self.surveys: list[Survey] = []
        # ship -> {"role", "until_credits"?, "until_ts"?}; a pin with a condition releases
        # itself (back to automatic roles + a re-plan) once the condition is met.
        self.operator_roles: dict[str, dict[str, Any]] = {}
        self._agent_refreshed_ts = 0.0
        self._fleet_synced_ts = 0.0
        self._snapshot_ts = 0.0
        self._task: asyncio.Task[None] | None = None
        self._system_cache: SystemWorld | None = None
        self.started_at = time.time()
        self.starting_credits = 0

    # ---------------------------------------------------------------- settings
    def update_settings(self, global_settings: Settings) -> None:
        self._global = global_settings

    @property
    def settings(self) -> Settings:
        return effective(self._global, self.overrides)

    @property
    def paused(self) -> bool:
        return self.settings.paused or not self.row.enabled

    @property
    def jev_enabled(self) -> bool:
        return self.settings.jev_enabled

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"agent:{self.symbol}")

    async def stop(self) -> None:
        for p in list(self.pilots.values()):
            await p.stop()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        try:
            await self.bootstrap()
        except Exception as e:
            log.exception("bootstrap failed for %s", self.symbol)
            await self.emit("error", f"bootstrap failed: {e}")
        while True:
            try:
                if not self.paused:
                    await self.supervise()
            except asyncio.CancelledError:
                raise
            except STError as e:
                await self.emit("error", f"supervisor: {e.message}")
            except Exception as e:
                log.exception("supervisor crashed for %s", self.symbol)
                await self.emit("error", f"supervisor crash: {e}")
            await asyncio.sleep(self.settings.tick_seconds)

    async def bootstrap(self) -> None:
        await self.refresh_agent()
        first_seen = await self.db.get_kv(f"starting_credits:{self.symbol}")
        if first_seen is None:
            first_seen = self.credits
            await self.db.set_kv(f"starting_credits:{self.symbol}", first_seen)
        self.starting_credits = int(first_seen)
        assert self.agent is not None
        await self.world.load_system(self.client, self.agent.headquarters.rsplit("-", 1)[0])
        await self.refresh_contracts()
        await self.restore_plan()
        stored = await self.db.get_kv(f"operator_roles:{self.symbol}") or {}
        self.operator_roles = {
            k: (v if isinstance(v, dict) else {"role": v}) for k, v in stored.items()
        }
        await self.sync_fleet()
        await self.emit("start", f"agent online with {len(self.pilots)} ships and {self.credits}c")

    async def restore_plan(self) -> None:
        """Resume the last persisted plan so a restart does not cost a strategist call."""
        runs = await self.db.list_strategist_runs(self.symbol, 1)
        if not runs:
            return
        try:
            self.plan = Plan.model_validate(runs[0]["plan"])
        except ValueError:
            return
        self.plan_ts = float(runs[0]["ts"])
        self.purchase_pending = False  # never resume a purchase blindly after a restart

    # ---------------------------------------------------------------- state
    async def emit(
        self,
        kind: str,
        message: str,
        *,
        ship: str | None = None,
        data: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> None:
        await self.bus.emit(kind, message, agent=self.symbol, ship=ship, data=data, persist=persist)

    async def refresh_agent(self) -> None:
        self.agent = await self.client.my_agent()
        self.credits = self.agent.credits

    async def on_credits(self, credits: int) -> None:
        self.credits = credits
        if self.agent:
            self.agent.credits = credits

    async def system_world(self) -> SystemWorld:
        assert self.agent is not None
        system = self.agent.headquarters.rsplit("-", 1)[0]
        return await self.world.load_system(self.client, system)

    @property
    def home_system(self) -> str:
        assert self.agent is not None
        return self.agent.headquarters.rsplit("-", 1)[0]

    async def sync_fleet(self) -> None:
        ships = await self.client.my_ships()
        seen = set()
        for ship in ships:
            seen.add(ship.symbol)
            pilot = self.pilots.get(ship.symbol)
            if pilot is None:
                pilot = ShipPilot(self, ship)
                self.pilots[ship.symbol] = pilot
                hint = self.plan.role_hints.get(ship.symbol) if self.plan else None
                pinned = (self.operator_roles.get(ship.symbol) or {}).get("role")
                if pinned and pinned in self.allowed_roles(pilot):
                    pilot.set_role(pinned, "operator")
                elif hint and hint in self.allowed_roles(pilot):
                    pilot.set_role(hint, "strategist")
                else:
                    pilot.set_role(self.default_role(pilot), "default")
                pilot.start()
            elif not pilot.in_step and ship.nav.status != "IN_TRANSIT":
                pilot.ship = ship  # only while the pilot is sleeping; it owns state mid-step
        for sym in list(self.pilots):
            if sym not in seen:
                await self.pilots.pop(sym).stop()

    def default_role(self, pilot: ShipPilot) -> str:
        s = pilot.ship
        if s.is_probe or s.cargo.capacity == 0:
            return "scout"
        if s.can_mine or s.can_siphon:
            if self.active_contract() is not None and self.can_work_contract(pilot):
                return "contract"
            return "mine"
        if len(self.miners()) >= 2:
            return "haul"
        if self.active_contract() is not None and self.can_work_contract(pilot):
            return "contract"
        return "trade"

    def allowed_roles(self, pilot: ShipPilot) -> list[str]:
        s = pilot.ship
        roles = ["idle", "scout"]
        if s.cargo.capacity > 0:
            roles.append("trade")
            if not (s.can_mine or s.can_siphon):
                roles.append("haul")
        if s.can_mine or s.can_siphon:
            roles.append("mine")
        if self.active_contract() is not None and self.can_work_contract(pilot):
            roles.append("contract")
        return roles

    def can_work_contract(self, pilot: ShipPilot) -> bool:
        c = self.active_contract()
        if c is None or pilot.ship.cargo.capacity == 0:
            return False
        for d in c.terms.deliver:
            if d.remaining <= 0:
                continue
            if d.trade_symbol in MINABLE_ORES and pilot.ship.can_mine:
                return True
            if d.trade_symbol in SIPHONABLE and pilot.ship.can_siphon:
                return True
            return True  # buyable: any cargo ship can haul it if a source is known
        return False

    async def refresh_contracts(self) -> None:
        for c in await self.client.my_contracts():
            self.contracts[c.id] = c

    def update_contract(self, contract: Contract) -> None:
        self.contracts[contract.id] = contract

    def active_contract(self) -> Contract | None:
        for c in self.contracts.values():
            if c.accepted and not c.fulfilled:
                return c
        return None

    # ---------------------------------------------------------------- surveys
    def usable_surveys(self, waypoint: str) -> list[Survey]:
        now = datetime.now(UTC)
        self.surveys = [x for x in self.surveys if x.expiration > now]
        return [x for x in self.surveys if x.symbol == waypoint]

    def add_surveys(self, found: list[Survey]) -> None:
        self.surveys.extend(found)

    def drop_survey(self, survey: Survey) -> None:
        self.surveys = [x for x in self.surveys if x.signature != survey.signature]

    @staticmethod
    def best_survey(surveys: list[Survey], wanted: set[str]) -> Survey | None:
        """Prefer surveys dense in wanted goods, then larger deposits."""
        if not surveys:
            return None
        size_rank = {"SMALL": 0, "MODERATE": 1, "LARGE": 2}

        def score(x: Survey) -> tuple[float, int]:
            deposits = [d.get("symbol") for d in x.deposits]
            hits = sum(1 for d in deposits if d in wanted)
            return (hits / max(len(deposits), 1), size_rank.get(x.size, 0))

        return max(surveys, key=score)

    # ---------------------------------------------------------------- hauling
    def miners(self) -> list[ShipPilot]:
        return [
            p
            for p in self.pilots.values()
            if p.role in ("mine", "contract") and (p.ship.can_mine or p.ship.can_siphon)
        ]

    def mining_hub(self) -> str | None:
        """The waypoint most miners are working (or heading to)."""
        sw = self.world.systems.get(self.home_system)
        if sw is None:
            return None
        counts: dict[str, int] = {}
        for p in self.miners():
            nav = p.ship.nav
            wp = nav.route.destination.symbol if nav.status == "IN_TRANSIT" else nav.waypoint_symbol
            w = sw.waypoints.get(wp)
            if w is not None and (w.is_asteroid or w.type == "GAS_GIANT"):
                counts[wp] = counts.get(wp, 0) + 1
        return max(counts, key=lambda k: counts[k]) if counts else None

    def miners_with_cargo(self, waypoint: str) -> list[ShipPilot]:
        return [
            p
            for p in self.miners()
            if p.ship.nav.waypoint_symbol == waypoint and p.ship.cargo.units > 0
        ]

    def hauler_at(self, waypoint: str) -> ShipPilot | None:
        for p in self.pilots.values():
            if (
                p.role == "haul"
                and p.ship.nav.status == "IN_ORBIT"
                and p.ship.nav.waypoint_symbol == waypoint
                and p.ship.cargo.free > 0
            ):
                return p
        return None

    def hauler_inbound(self, waypoint: str) -> ShipPilot | None:
        for p in self.pilots.values():
            if p.role != "haul":
                continue
            nav = p.ship.nav
            if nav.status == "IN_TRANSIT" and nav.route.destination.symbol == waypoint:
                return p
            if nav.waypoint_symbol == waypoint and p.ship.cargo.free > 0:
                return p  # here but docked / mid-step; usable shortly
        return None

    async def set_operator_role(
        self,
        pilot: ShipPilot,
        role: str | None,
        *,
        until_credits: int | None = None,
        until_minutes: float | None = None,
    ) -> None:
        """Pin (or with None, unpin) a ship's role from the UI; persisted across restarts.

        With `until_credits` / `until_minutes` the pin is temporary: it releases itself when the
        agent's credits reach the target or the time elapses, and the strategist re-plans.
        """
        if role is None:
            self.operator_roles.pop(pilot.ship.symbol, None)
            pilot.set_role(self.default_role(pilot), "default")
        else:
            pin: dict[str, Any] = {"role": role}
            if until_credits:
                pin["until_credits"] = int(until_credits)
            if until_minutes:
                pin["until_ts"] = time.time() + float(until_minutes) * 60
            self.operator_roles[pilot.ship.symbol] = pin
            pilot.set_role(role, "operator")
        await self.db.set_kv(f"operator_roles:{self.symbol}", self.operator_roles)

    async def release_expired_pins(self) -> None:
        for sym, pin in list(self.operator_roles.items()):
            pilot = self.pilots.get(sym)
            if pilot is None:
                continue
            reason = None
            if pin.get("until_credits") and self.credits >= int(pin["until_credits"]):
                reason = f"credits reached {self.credits:,}"
            elif pin.get("until_ts") and time.time() >= float(pin["until_ts"]):
                reason = "time limit reached"
            if reason is None:
                continue
            # Let an in-flight trade finish: release only when the hold is empty of trade goods.
            if pilot.role == "trade" and sym in self.trade_plans:
                continue
            await self.set_operator_role(pilot, None)
            await self.emit(
                "roles",
                f"released {sym} from pinned '{pin['role']}' ({reason}); back to automatic roles",
                ship=sym,
            )
            self.request_replan(f"pin released on {sym}")

    def contract_goods(self) -> set[str]:
        c = self.active_contract()
        return {d.trade_symbol for d in c.terms.deliver if d.remaining > 0} if c else set()

    def schedule(self, coro: Any) -> None:
        """Run a coroutine in the background from synchronous code (errors are logged)."""
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def request_replan(self, reason: str) -> None:
        self.replan_reason = reason

    # ---------------------------------------------------------------- supervisor
    async def supervise(self) -> None:
        # Keep the supervisor cheap: pilots already report credits after every transaction and
        # new ships only appear when we buy them, so poll the server sparingly.
        now = time.time()
        if now - self._agent_refreshed_ts >= 30:
            await self.refresh_agent()
            self._agent_refreshed_ts = now
        if now - self._fleet_synced_ts >= 60:
            await self.sync_fleet()
            self._fleet_synced_ts = now
        if now - self._snapshot_ts >= 30:
            await self.db.add_snapshot(self.symbol, self.credits, len(self.pilots))
            self._snapshot_ts = now
        for p in self.pilots.values():
            await p.flush_stint(force=False)
        await self.release_expired_pins()
        await self.maybe_accept_contract()
        await self.maybe_negotiate_contract()
        await self.maybe_replan()
        await self.maybe_assign_roles()
        await self.maybe_buy_ship()

    async def maybe_accept_contract(self) -> None:
        """Accepting is strictly dominant: it pays `on_accepted`, there is no penalty for not
        finishing, and an unaccepted offer blocks negotiating a new one just the same. How hard
        the fleet works it is decided by role assignment using `contract_economics`."""
        if self.active_contract() is not None:
            return
        if time.time() - self._contracts_checked_ts < 60:
            return
        self._contracts_checked_ts = time.time()
        await self.refresh_contracts()
        for c in self.contracts.values():
            if c.accepted or c.fulfilled:
                continue
            info = contract_summary(c)
            updated = await self.client.accept_contract(c.id)
            self.update_contract(updated)
            await self.refresh_agent()
            econ = await self.contract_economics()
            await self.emit(
                "contract_accepted",
                f"accepted contract {c.id} ({info['deliver']}); "
                f"payout {econ.get('payout_remaining')} vs market {econ.get('market_value_of_goods')}",
                data={**info, **econ},
            )
            self.request_replan("contract accepted")
            for p in self.pilots.values():
                if p.role == "mine" and self.can_work_contract(p) and econ.get("worth_it", True):
                    p.set_role("contract", "new contract")
            return

    async def contract_economics(self) -> dict[str, Any]:
        """Is finishing the active contract better than selling the same goods on the market?"""
        c = self.active_contract()
        if c is None:
            return {}
        remaining = {d.trade_symbol: d.remaining for d in c.terms.deliver if d.remaining > 0}
        rows = await self.world.fresh_rows(self.home_system)
        best_sell: dict[str, int] = {}
        for r in rows:
            if r["good"] in remaining and r["sell"]:
                best_sell[r["good"]] = max(best_sell.get(r["good"], 0), int(r["sell"]))
        market_value = sum(best_sell.get(g, 0) * n for g, n in remaining.items())
        known = all(g in best_sell for g in remaining)
        payout = c.terms.payment.on_fulfilled
        days = (c.terms.deadline - datetime.now(UTC)).total_seconds() / 86400
        miners = [p for p in self.pilots.values() if p.ship.can_mine or p.ship.can_siphon]
        can_mine = all(
            (g in MINABLE_ORES and any(p.ship.can_mine for p in miners))
            or (g in SIPHONABLE and any(p.ship.can_siphon for p in miners))
            for g in remaining
        )
        return {
            "units_remaining": sum(remaining.values()),
            "goods": list(remaining),
            "payout_remaining": payout,
            "market_value_of_goods": market_value if known else None,
            "premium_over_market": (payout - market_value) if known else None,
            "days_to_deadline": round(days, 1),
            "fleet_can_mine_goods": bool(remaining) and can_mine,
            # Worth it unless the market pays clearly more for the same goods.
            "worth_it": (not known) or payout >= market_value * 0.8,
        }

    async def maybe_negotiate_contract(self) -> None:
        """With no contract offered or ongoing, negotiate one at a faction waypoint."""
        if any(not c.fulfilled for c in self.contracts.values()):
            return
        if time.time() - self._negotiate_ts < 120:
            return
        self._negotiate_ts = time.time()
        sw = await self.system_world()
        faction_wps = {w.symbol for w in sw.waypoints.values() if w.faction is not None}
        for p in self.pilots.values():
            if p.ship.nav.status == "DOCKED" and p.wp in faction_wps and not p.in_step:
                await self.try_negotiate(p)
                return
        if any(p.errand for p in self.pilots.values()):
            return
        runner = self._pick_errand_ship()
        if runner is None:
            return
        target = min(faction_wps, key=lambda w: sw.dist(runner.wp, w)) if faction_wps else None
        if target is None:
            target = self.agent.headquarters if self.agent else None
        if target:
            runner.errand = ("negotiate", "", target)
            runner._wake.set()

    async def try_negotiate(self, pilot: ShipPilot) -> None:
        try:
            c = await self.client.negotiate_contract(pilot.ship.symbol)
        except STError as e:
            await self.emit("info", f"negotiate contract failed at {pilot.wp}: {e.message}")
            return
        self.update_contract(c)
        await self.emit(
            "contract_offered",
            f"negotiated contract {c.id}",
            data=contract_summary(c),
            ship=pilot.ship.symbol,
        )
        self._contracts_checked_ts = 0.0  # accept on the next tick

    async def maybe_replan(self) -> None:
        interval = self.settings.strategist_interval_minutes * 60
        due = time.time() - self.plan_ts >= interval
        if not due and self.replan_reason is None:
            return
        reason = self.replan_reason or ("initial" if self.plan is None else "interval")
        self.replan_reason = None
        await self.run_strategist(reason)

    async def run_strategist(self, reason: str) -> None:
        s = self.settings
        summary = await self.build_summary()
        previous = self.plan.notes_for_next_time if self.plan else ""
        await self.emit(
            "strategist_start", f"strategist thinking ({reason}, {s.thinking_rounds} round(s))"
        )
        try:
            plan, models, cost = await self.strategist.plan(
                agent=self.symbol,
                summary=summary,
                strategist_model=s.strategist_model,
                critic_model=s.critic_model,
                rounds=s.thinking_rounds,
                budget_usd=s.monthly_llm_budget_usd,
                max_tokens=s.strategist_max_tokens,
                reasoning=s.strategist_reasoning,
                operator_notes=self.overrides.operator_notes,
                previous_notes=previous,
            )
        except BudgetExceededError as e:
            self.plan_error = str(e)
            self.plan_ts = time.time()  # back off for a full interval
            await self.emit("budget", f"strategist skipped: {e}")
            return
        except Exception as e:
            self.plan_error = str(e)
            self.plan_ts = time.time() - interval_backoff(s)
            await self.emit("error", f"strategist failed: {e}")
            return
        if plan is None:
            self.plan_error = f"unparseable output from {models[-1]}"
            self.plan_ts = time.time() - interval_backoff(s)
            await self.emit(
                "error",
                f"strategist returned an unparseable plan (${cost:.4f}); keeping the old one",
            )
            return
        self.plan = plan
        self.plan_ts = time.time()
        self.plan_error = None
        self.purchase_pending = plan.ship_purchase is not None
        await self.db.replace_goals(self.symbol, "long", [g.model_dump() for g in plan.goals])
        await self.db.add_strategist_run(
            self.symbol, s.thinking_rounds, models, str(summary)[:4000], plan.model_dump(), cost
        )
        await self.emit(
            "plan",
            f"new plan (${cost:.4f}, {', '.join(models)}): {plan.assessment[:200]}",
            data=plan.model_dump(),
        )
        for sym, role in plan.role_hints.items():
            p = self.pilots.get(sym)
            if p and p.role_source != "operator" and role in self.allowed_roles(p):
                p.set_role(role, "strategist")
        self.last_roles_ts = time.time()

    async def maybe_assign_roles(self) -> None:
        if time.time() - self.last_roles_ts < 300:
            return
        self.last_roles_ts = time.time()
        allowed = {sym: self.allowed_roles(p) for sym, p in self.pilots.items()}
        if not self.jev_enabled:
            for sym, p in self.pilots.items():
                if p.role not in allowed[sym]:
                    p.set_role(self.default_role(p), "heuristic")
            return
        goals = await self.db.list_goals(self.symbol)
        memory = await self.memory.summary()
        picks = await self.jev.assign_roles(
            agent=self.symbol,
            fleet=[p.snapshot() for p in self.pilots.values()],
            goals=[
                {"kind": g["kind"], "description": g["description"], "priority": g["priority"]}
                for g in goals
            ],
            context={
                **await self.situation(),
                "recent_outcomes": {
                    "per_role": memory["roles"],
                    "per_ship": memory["ship_history"],
                    "blocked": memory["blocked"],
                },
            },
            allowed=allowed,
        )
        changed = []
        for sym, (role, conf) in picks.items():
            p = self.pilots.get(sym)
            if p is None or role == p.role or role not in allowed[sym]:
                continue
            # Operator picks are sticky; strategist hints yield only when Jev is fairly sure.
            if p.role_source == "operator" or (p.role_source == "strategist" and conf < 0.6):
                continue
            p.set_role(role, f"jev ({conf:.2f})")
            changed.append(f"{sym}→{role}")
        if changed:
            await self.emit("roles", "Jev reassigned roles: " + ", ".join(changed))

    async def maybe_buy_ship(self) -> None:
        if not self.purchase_pending or self.plan is None or self.plan.ship_purchase is None:
            return
        if not self.settings.allow_ship_purchases:
            return
        if any(p.errand for p in self.pilots.values()):
            return
        req = self.plan.ship_purchase
        if len(self.pilots) >= self.settings.max_ships_to_buy + 2:
            self.purchase_pending = False
            return
        if self.credits < req.when_credits_above:
            return
        sw = await self.system_world()
        offers = [
            (wp, listing["price"])
            for wp, listings in sw.shipyards.items()
            for listing in listings
            if listing["type"] == req.ship_type and listing.get("price")
        ]
        if not offers:
            # Need a price: send a scout to the nearest shipyard that lists the type.
            yards = [
                wp
                for wp, listings in sw.shipyards.items()
                if any(listing["type"] == req.ship_type for listing in listings)
            ] or [w.symbol for w in sw.shipyard_waypoints()]
            if not yards:
                await self.emit("info", f"no shipyard known for {req.ship_type}")
                self.purchase_pending = False
                return
            scout = self._pick_errand_ship()
            if scout:
                target = min(yards, key=lambda y: sw.dist(scout.wp, y))
                scout.errand = ("purchase", req.ship_type, target)
                scout._wake.set()
            return
        yard, price = min(offers, key=lambda o: o[1])
        if self.credits - price < self.settings.min_credit_reserve:
            return
        go = True
        if self.jev_enabled:
            prob = await self.jev.judge(
                agent=self.symbol,
                purpose="buy_ship_now",
                instructions=f"Should the agent buy a {req.ship_type} for {price} credits right now?",
                situation={
                    **await self.situation(),
                    "price": price,
                    "reason": req.reason,
                    "credits_after": self.credits - price,
                },
                yes="Purchase is affordable and advances the goals",
                no="Wait: reserve too thin or the ship would not be useful yet",
            )
            go = prob is None or prob >= 0.5
        if not go:
            return
        ship = self._pick_errand_ship(prefer_at=yard)
        if ship is None:
            return
        if ship.wp == yard and ship.ship.nav.status != "IN_TRANSIT":
            await self.try_purchase_ship(req.ship_type, yard)
        else:
            ship.errand = ("purchase", req.ship_type, yard)
            ship._wake.set()

    def _pick_errand_ship(self, prefer_at: str | None = None) -> ShipPilot | None:
        """Probe first (it travels free and has nothing better to do), then whoever is closest."""
        if not self.pilots:
            return None
        if prefer_at:
            here = [
                p
                for p in self.pilots.values()
                if p.wp == prefer_at and p.ship.nav.status != "IN_TRANSIT"
            ]
            if here:
                return here[0]
        probes = [p for p in self.pilots.values() if p.ship.is_probe]
        if probes:
            return probes[0]
        cands = [p for p in self.pilots.values() if p.ship.nav.status != "IN_TRANSIT"]
        if not cands:
            return None
        if prefer_at:
            sw = self.world.systems.get(self.home_system)
            if sw:
                return min(cands, key=lambda p: sw.dist(p.wp, prefer_at))
        return cands[0]

    async def try_purchase_ship(self, ship_type: str, yard: str) -> None:
        try:
            data = await self.client.purchase_ship(ship_type, yard)
        except STError as e:
            await self.emit("error", f"purchase {ship_type} failed: {e.message}")
            self.purchase_pending = False
            return
        await self.on_credits(data["agent"]["credits"])
        tx = data.get("transaction", {})
        await self.emit(
            "ship_purchased",
            f"bought {ship_type} ({data['ship']['symbol']}) for {tx.get('price')}c",
            data={"type": ship_type, "ship": data["ship"]["symbol"], "price": tx.get("price")},
        )
        assert self.plan and self.plan.ship_purchase
        self.plan.ship_purchase.count -= 1
        if self.plan.ship_purchase.count <= 0:
            self.purchase_pending = False
        await self.sync_fleet()
        self._fleet_synced_ts = time.time()
        self.request_replan("ship purchased")

    # ---------------------------------------------------------------- summaries
    async def situation(self) -> dict[str, Any]:
        c = self.active_contract()
        self._known_export_goods = {
            r["good"] for r in await self.world.fresh_rows(self.home_system) if r["buy"]
        }
        stale = await self.world.market_staleness(self.home_system)
        fresh = sum(1 for v in stale.values() if v < 3 * 3600)
        return {
            "credits": self.credits,
            "reserve": self.settings.min_credit_reserve,
            "ships": len(self.pilots),
            "active_contract": contract_summary(c) if c else None,
            "contract_economics": await self.contract_economics(),
            "markets_known": f"{fresh}/{len(stale)} fresh",
            "best_routes": [
                r.as_dict() for r in await self.world.best_routes(self.home_system, 40, 3)
            ],
        }

    async def build_summary(self) -> dict[str, Any]:
        sw = await self.system_world()
        goals = await self.db.list_goals(self.symbol)
        events = await self.db.list_events(self.symbol, limit=15)
        return {
            "agent": self.symbol,
            "credits": self.credits,
            "credits_at_start": self.starting_credits,
            "hours_running": round((time.time() - self.started_at) / 3600, 1),
            "settings": {
                "max_ships": self.settings.max_ships_to_buy,
                "credit_reserve": self.settings.min_credit_reserve,
            },
            "home_system": self.home_system,
            "fleet": [
                {
                    k: v
                    for k, v in p.snapshot().items()
                    if k not in ("last_error", "role_source", "speed")
                }
                for p in self.pilots.values()
            ],
            "contracts": [contract_summary(c) for c in self.contracts.values() if not c.fulfilled],
            "contract_economics": await self.contract_economics(),
            "contracts_fulfilled": sum(1 for c in self.contracts.values() if c.fulfilled),
            "system": {
                "asteroids": [w.symbol + ":" + w.type for w in sw.asteroids()][:6],
                "gas_giants": [w.symbol for w in sw.gas_giants()][:3],
                "markets": len(sw.markets()),
                "shipyards": {
                    wp: [f"{x['type']}={x.get('price')}" for x in listings]
                    for wp, listings in sw.shipyards.items()
                },
            },
            "market_knowledge": await self.situation(),
            "neighbour_systems": await self.neighbour_summary(),
            "tactical_memory": await self.memory.summary(),
            "current_goals": [
                {"kind": g["kind"], "description": g["description"], "priority": g["priority"]}
                for g in goals
            ],
            "recent_events": [e["message"][:120] for e in events],
            "last_plan_error": self.plan_error,
        }

    async def neighbour_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        sw = await self.system_world()
        gate = sw.gate()
        out["home_gate"] = (
            None
            if gate is None
            else f"{gate.symbol} ({'under construction' if gate.is_under_construction else 'usable'})"
        )
        try:
            systems = await self.world.neighbours(
                self.client, self.home_system, self.settings.explore_systems
            )
        except STError as e:
            out["error"] = e.message
            return out
        for sy in systems:
            stale = await self.world.market_staleness(sy)
            nsw = self.world.systems[sy]
            out[sy] = {
                "markets": f"{sum(1 for v in stale.values() if v < 3 * 3600)}/{len(stale)} fresh",
                "shipyards": {
                    wp: [f"{x['type']}={x.get('price')}" for x in listings][:6]
                    for wp, listings in nsw.shipyards.items()
                },
                "asteroids": len(nsw.asteroids()),
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "faction": self.row.faction,
            "headquarters": self.row.headquarters,
            "enabled": self.row.enabled,
            "paused": self.paused,
            "credits": self.credits,
            "starting_credits": self.starting_credits,
            "ships": [p.snapshot() for p in self.pilots.values()],
            "contracts": [contract_summary(c) for c in self.contracts.values()],
            "plan": self.plan.model_dump() if self.plan else None,
            "plan_ts": self.plan_ts,
            "plan_error": self.plan_error,
            "purchase_pending": self.purchase_pending,
            "overrides": self.overrides.model_dump(),
            "effective_settings": self.settings.model_dump(),
            "requests": self.client.request_count,
        }


def contract_summary(c: Contract) -> dict[str, Any]:
    return {
        "id": c.id,
        "type": c.type,
        "accepted": c.accepted,
        "fulfilled": c.fulfilled,
        "deadline": c.terms.deadline.isoformat(),
        "payment": c.terms.payment.on_accepted + c.terms.payment.on_fulfilled,
        "deliver": [
            f"{d.units_fulfilled}/{d.units_required} {d.trade_symbol} → {d.destination_symbol}"
            for d in c.terms.deliver
        ],
    }


def interval_backoff(s: Settings) -> float:
    """After a strategist failure, retry after 5 minutes rather than the full interval."""
    return max(0.0, s.strategist_interval_minutes * 60 - 300)
