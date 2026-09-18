import time
from pathlib import Path

import pytest

from spacebrains.brain.openrouter import extract_json
from spacebrains.brain.strategist import Plan
from spacebrains.db import Database
from spacebrains.game.world import SystemWorld, World, fuel_cost
from spacebrains.settings import AgentOverrides, Settings, effective
from spacebrains.st.client import RateLimiter, system_of
from spacebrains.st.models import Waypoint


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": {"b": [1, 2]}} hope that helps') == {
        "a": {"b": [1, 2]}
    }


def test_plan_validation_rejects_bad_roles():
    plan = Plan.model_validate(
        {"assessment": "x", "goals": [{"kind": "custom", "description": "d"}]}
    )
    assert plan.goals[0].priority == 5
    with pytest.raises(ValueError):
        Plan.model_validate({"role_hints": {"S-1": "pirate"}})


def test_effective_settings_merge():
    base = Settings()
    merged = effective(base, AgentOverrides(thinking_rounds=3, operator_notes="hi"))
    assert merged.thinking_rounds == 3
    assert merged.strategist_model == base.strategist_model
    assert effective(base, None) is base


def test_system_of_and_fuel():
    assert system_of("X1-PJ58-A1") == "X1-PJ58"
    assert fuel_cost(0, "DRIFT") == 1
    assert fuel_cost(37.2, "CRUISE") == 37
    assert fuel_cost(10, "BURN") == 20


async def test_rate_limiter_paces_requests():
    rl = RateLimiter(rate=50, burst=2)
    t0 = time.monotonic()
    for _ in range(6):
        await rl.acquire()
    assert time.monotonic() - t0 >= 4 / 50 * 0.8


def _wp(sym: str, x: int, y: int, traits: list[str] | None = None, typ: str = "PLANET") -> Waypoint:
    return Waypoint.model_validate(
        {
            "symbol": sym,
            "type": typ,
            "systemSymbol": "X1-T",
            "x": x,
            "y": y,
            "traits": [{"symbol": t, "name": t} for t in traits or []],
        }
    )


async def test_best_routes_and_sell_markets(tmp_path: Path):
    db = Database(tmp_path / "t.sqlite3")
    await db.open()
    world = World(db)
    sw = SystemWorld(symbol="X1-T", loaded_at=time.time())
    sw.waypoints = {
        "X1-T-A": _wp("X1-T-A", 0, 0, ["MARKETPLACE"]),
        "X1-T-B": _wp("X1-T-B", 30, 40, ["MARKETPLACE"]),
        "X1-T-C": _wp("X1-T-C", 5, 5, [], "ENGINEERED_ASTEROID"),
    }
    world.systems["X1-T"] = sw
    await db.upsert_market(
        "X1-T-A",
        [
            {
                "symbol": "IRON_ORE",
                "type": "EXPORT",
                "purchasePrice": 40,
                "sellPrice": 30,
                "tradeVolume": 20,
                "supply": "HIGH",
            },
            {
                "symbol": "FUEL",
                "type": "EXCHANGE",
                "purchasePrice": 80,
                "sellPrice": 70,
                "tradeVolume": 100,
                "supply": "HIGH",
            },
        ],
    )
    await db.upsert_market(
        "X1-T-B",
        [
            {
                "symbol": "IRON_ORE",
                "type": "IMPORT",
                "purchasePrice": 120,
                "sellPrice": 100,
                "tradeVolume": 10,
                "supply": "SCARCE",
            },
        ],
    )
    routes = await world.best_routes("X1-T", capacity=40)
    assert routes and routes[0].good == "IRON_ORE"
    assert routes[0].buy_at == "X1-T-A" and routes[0].sell_at == "X1-T-B"
    assert routes[0].margin == 60 and routes[0].volume == 10

    sells = await world.best_sell_markets("X1-T", ["IRON_ORE"], origin="X1-T-C")
    assert sells[0]["waypoint"] == "X1-T-B"
    assert sells[0]["prices"] == {"IRON_ORE": 100}

    src = await world.cheapest_source("X1-T", "IRON_ORE")
    assert src == {"waypoint": "X1-T-A", "price": 40, "volume": 20}
    stale = await world.market_staleness("X1-T")
    assert set(stale) == {"X1-T-A", "X1-T-B"} and all(v < 5 for v in stale.values())
    assert sw.asteroids()[0].symbol == "X1-T-C"
    await db.close()
