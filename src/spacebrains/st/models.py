"""Typed subset of the SpaceTraders v2 API models (camelCase on the wire, snake_case here)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class STModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class Agent(STModel):
    symbol: str
    headquarters: str
    credits: int
    starting_faction: str
    ship_count: int = 0


class WaypointTrait(STModel):
    symbol: str
    name: str = ""


class Waypoint(STModel):
    symbol: str
    type: str
    system_symbol: str
    x: int
    y: int
    traits: list[WaypointTrait] = Field(default_factory=list)
    orbits: str | None = None
    is_under_construction: bool = False

    def has_trait(self, trait: str) -> bool:
        return any(t.symbol == trait for t in self.traits)

    @property
    def is_market(self) -> bool:
        return self.has_trait("MARKETPLACE")

    @property
    def is_shipyard(self) -> bool:
        return self.has_trait("SHIPYARD")

    @property
    def is_asteroid(self) -> bool:
        return self.type in {"ASTEROID", "ENGINEERED_ASTEROID", "ASTEROID_FIELD"}


class System(STModel):
    symbol: str
    sector_symbol: str = ""
    type: str = ""
    x: int = 0
    y: int = 0


class ShipNavRouteWaypoint(STModel):
    symbol: str
    type: str = ""
    system_symbol: str = ""
    x: int = 0
    y: int = 0


class ShipNavRoute(STModel):
    destination: ShipNavRouteWaypoint
    origin: ShipNavRouteWaypoint
    departure_time: datetime
    arrival: datetime


class ShipNav(STModel):
    system_symbol: str
    waypoint_symbol: str
    route: ShipNavRoute
    status: str  # IN_TRANSIT | IN_ORBIT | DOCKED
    flight_mode: str  # DRIFT | STEALTH | CRUISE | BURN

    def seconds_to_arrival(self) -> float:
        if self.status != "IN_TRANSIT":
            return 0.0
        return max(0.0, (self.route.arrival - datetime.now(UTC)).total_seconds())


class ShipFuel(STModel):
    current: int
    capacity: int


class CargoItem(STModel):
    symbol: str
    name: str = ""
    units: int


class ShipCargo(STModel):
    capacity: int
    units: int
    inventory: list[CargoItem] = Field(default_factory=list)

    def units_of(self, symbol: str) -> int:
        return sum(i.units for i in self.inventory if i.symbol == symbol)

    @property
    def free(self) -> int:
        return self.capacity - self.units


class ShipMount(STModel):
    symbol: str
    name: str = ""


class ShipModule(STModel):
    symbol: str
    name: str = ""


class ShipRegistration(STModel):
    name: str
    faction_symbol: str = ""
    role: str


class ShipFrame(STModel):
    symbol: str
    condition: float = 1.0
    integrity: float = 1.0


class ShipEngine(STModel):
    symbol: str
    speed: int = 1
    condition: float = 1.0


class Cooldown(STModel):
    ship_symbol: str
    total_seconds: int
    remaining_seconds: int
    expiration: datetime | None = None


class Ship(STModel):
    symbol: str
    registration: ShipRegistration
    nav: ShipNav
    frame: ShipFrame
    engine: ShipEngine
    cargo: ShipCargo
    fuel: ShipFuel
    cooldown: Cooldown
    mounts: list[ShipMount] = Field(default_factory=list)
    modules: list[ShipModule] = Field(default_factory=list)

    def has_mount(self, prefix: str) -> bool:
        return any(m.symbol.startswith(prefix) for m in self.mounts)

    @property
    def can_mine(self) -> bool:
        return self.has_mount("MOUNT_MINING_LASER")

    @property
    def can_siphon(self) -> bool:
        return self.has_mount("MOUNT_GAS_SIPHON")

    @property
    def can_survey(self) -> bool:
        return self.has_mount("MOUNT_SURVEYOR")

    @property
    def is_probe(self) -> bool:
        return self.frame.symbol == "FRAME_PROBE"

    @property
    def cooldown_remaining(self) -> float:
        if self.cooldown.expiration is None:
            return float(self.cooldown.remaining_seconds)
        return max(0.0, (self.cooldown.expiration - datetime.now(UTC)).total_seconds())


class ContractDeliverable(STModel):
    trade_symbol: str
    destination_symbol: str
    units_required: int
    units_fulfilled: int

    @property
    def remaining(self) -> int:
        return self.units_required - self.units_fulfilled


class ContractPayment(STModel):
    on_accepted: int
    on_fulfilled: int


class ContractTerms(STModel):
    deadline: datetime
    payment: ContractPayment
    deliver: list[ContractDeliverable] = Field(default_factory=list)


class Contract(STModel):
    id: str
    faction_symbol: str
    type: str
    terms: ContractTerms
    accepted: bool
    fulfilled: bool
    deadline_to_accept: datetime | None = None


class MarketTradeGood(STModel):
    symbol: str
    type: str  # EXPORT | IMPORT | EXCHANGE
    trade_volume: int
    supply: str
    activity: str | None = None
    purchase_price: int
    sell_price: int


class MarketGoodRef(STModel):
    symbol: str
    name: str = ""


class Market(STModel):
    symbol: str
    exports: list[MarketGoodRef] = Field(default_factory=list)
    imports: list[MarketGoodRef] = Field(default_factory=list)
    exchange: list[MarketGoodRef] = Field(default_factory=list)
    trade_goods: list[MarketTradeGood] | None = None

    def all_goods(self) -> set[str]:
        return {g.symbol for g in [*self.exports, *self.imports, *self.exchange]}


class ShipyardShip(STModel):
    type: str
    name: str = ""
    purchase_price: int
    supply: str = ""


class ShipyardTypeRef(STModel):
    type: str


class Shipyard(STModel):
    symbol: str
    ship_types: list[ShipyardTypeRef] = Field(default_factory=list)
    ships: list[ShipyardShip] | None = None
    modifications_fee: int = 0


class Survey(STModel):
    signature: str
    symbol: str
    deposits: list[dict[str, Any]] = Field(default_factory=list)
    expiration: datetime
    size: str


class Transaction(STModel):
    waypoint_symbol: str = ""
    ship_symbol: str = ""
    trade_symbol: str = ""
    type: str = ""
    units: int = 0
    price_per_unit: int = 0
    total_price: int = 0
