"""Domain models shared by ingest, solver, validator, editor, exports and UI.

Vocabulary follows the build brief glossary exactly: Shipment, Stop, Load, Truck,
Plan, plan version, order type, tier.  Times inside plans are stored as *seconds
since midnight of the delivery day D* (integers) so that arithmetic is exact and
JSON-friendly; use :func:`clock` to render them as local clock times.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Hub (fixed for the pilot)
# --------------------------------------------------------------------------------------

# Coordinates: ArcGIS point-address match for the street address, confirmed against the
# OpenStreetMap road network (nearest road = Mango Avenue, < 1 m).  The US Census geocoder
# has no address ranges on this part of Mango Ave, so the hub cannot be geocoded at run time.
HUB_ADDRESS = "5550 Mango Ave, Rialto, CA 92377"
HUB_LAT = 34.1511
HUB_LON = -117.4319
HUB_TIMEZONE = "America/Los_Angeles"


@dataclass(frozen=True)
class Site:
    """A loading site (one or more DC codes at one address).  Every site is a depot node."""

    code: str
    name: str
    address: str
    lat: float
    lon: float
    dc_codes: tuple[str, ...]


# CA LFLT: LGC2 and LGC7 are two DCs at one address (5550 Mango Ave, Rialto) - a load is
# picked up at one DC only, and moving between them for a second turn takes no driving.
# LGC8 is 5267 Sierra Ave, Fontana (OpenStreetMap house-number point).  The first site is
# the default for rows without a DC.
SITES: tuple[Site, ...] = (
    Site("LGC2", "LGC2", HUB_ADDRESS, HUB_LAT, HUB_LON, ("LGC2",)),
    Site("LGC7", "LGC7", HUB_ADDRESS, HUB_LAT, HUB_LON, ("LGC7",)),
    Site(
        "LGC8",
        "LGC8",
        "5267 Sierra Ave, Fontana, CA 92336",
        34.1441316,
        -117.4359977,
        ("LGC8",),
    ),
)
DEFAULT_SITE = SITES[0].code
N_DEPOTS = len(SITES)
SITE_NODE: dict[str, int] = {s.code: i for i, s in enumerate(SITES)}
SITE_BY_CODE: dict[str, Site] = {s.code: s for s in SITES}
DC_TO_SITE: dict[str, str] = {dc: s.code for s in SITES for dc in s.dc_codes}
# site codes of runs saved when LGC2 and LGC7 were one site
LEGACY_SITE_CODES: dict[str, str] = {"RIALTO": "LGC2", "FONTANA": "LGC8"}


def site_for_dc(dc: str | None) -> str | None:
    """Site code for a DC code (LGC2, LGC7, LGC8); None if unknown."""
    if not dc:
        return DEFAULT_SITE
    return DC_TO_SITE.get(dc.strip().upper())


def normalize_site(code: str) -> str:
    return LEGACY_SITE_CODES.get(code, code)


def hub_now() -> dt.datetime:
    """Current wall-clock time *at the hub*, as a naive datetime.

    Every "today", run date and audit timestamp in RouteOpt uses this so a planner (or a
    server) in another time zone still plans and logs in the hub's clock.  Falls back to the
    machine's local time if the zone database is unavailable.
    """
    try:
        return dt.datetime.now(ZoneInfo(HUB_TIMEZONE)).replace(tzinfo=None)
    except ZoneInfoNotFoundError:  # pragma: no cover - needs a machine without tzdata
        log.warning("Time zone %s not found; using the machine's local time", HUB_TIMEZONE)
        return dt.datetime.now()


def hub_today() -> dt.date:
    """Today's date at the hub (the planning day, D-2)."""
    return hub_now().date()


class ShipmentType(StrEnum):
    """Type of a shipment, taken from the ``shipment_id`` suffix."""

    LMD = "LMD"  # delivery (may include installation) - consumes truck volume hub -> stop
    RTN = "RTN"  # return pickup - adds volume stop -> hub
    INC = "INC"  # installation check - zero volume, service time only


class OrderType(StrEnum):
    OBS = "OBS"  # direct-to-consumer homes, highest priority
    BUILDER = "BUILDER"
    LTL = "LTL"  # consignee, lowest priority


class DayTag(StrEnum):
    D = "D"
    D1 = "D+1"
    D2 = "D+2"

    @property
    def offset(self) -> int:
        return {"D": 0, "D+1": 1, "D+2": 2}[self.value]

    @classmethod
    def from_offset(cls, offset: int) -> DayTag:
        return {0: cls.D, 1: cls.D1, 2: cls.D2}[offset]


# --------------------------------------------------------------------------------------
# Input side
# --------------------------------------------------------------------------------------


class Shipment(BaseModel):
    """One Excel row: one model code x pieces."""

    model_config = ConfigDict(frozen=True)

    shipment_id: str
    shipment_type: ShipmentType
    purchase_order: str | None = None
    delivery_date: dt.date
    day_tag: DayTag
    order_type: OrderType
    customer_name: str
    phone: str | None = None
    address: str
    city: str
    state: str
    zip: str
    latitude: float | None = None
    longitude: float | None = None
    model_code: str | None = None
    pieces: int = 0
    weight_lb: float = 0.0
    volume_cuft: float = 0.0
    service_minutes: int = 0
    notes: str | None = None
    source_row: int = 0
    origin_dc: str | None = None  # LGC2 / LGC7 / LGC8 (CA); None = default site
    site: str = DEFAULT_SITE
    pallet_required: bool = False
    priority: int | None = None  # CA: 1 existing LFLT, 2 add-on, 3 low

    @field_validator("site")
    @classmethod
    def _known_site(cls, v: str) -> str:
        return normalize_site(v)

    @property
    def delivery_volume(self) -> float:
        return self.volume_cuft if self.shipment_type == ShipmentType.LMD else 0.0

    @property
    def pickup_volume(self) -> float:
        return self.volume_cuft if self.shipment_type == ShipmentType.RTN else 0.0

    @property
    def delivery_weight(self) -> float:
        return self.weight_lb if self.shipment_type == ShipmentType.LMD else 0.0

    @property
    def pickup_weight(self) -> float:
        return self.weight_lb if self.shipment_type == ShipmentType.RTN else 0.0


class Stop(BaseModel):
    """All shipments sharing one address (address + zip key), served together or not at all."""

    model_config = ConfigDict(frozen=True)

    stop_id: str
    key: str
    address: str
    city: str
    state: str
    zip: str
    latitude: float
    longitude: float
    customer_names: list[str]
    phones: list[str]
    shipments: list[Shipment]
    purchase_orders: list[str]
    order_types: list[OrderType]
    delivery_volume: float
    pickup_volume: float
    delivery_weight: float
    pickup_weight: float
    pieces_in: int  # delivered pieces (leave the truck)
    pieces_out: int  # picked-up pieces (board the truck)
    service_total_minutes: int  # sum of shipment service + stop buffer, once per stop
    day_tag: DayTag
    tier: int = Field(ge=1, le=5)
    site: str = DEFAULT_SITE
    pallet: bool = False  # any shipment needs a pallet -> the load uses the pallet limit
    contact: str = ""  # appointment contact (Memo Text first, else look up by address)

    @field_validator("site")
    @classmethod
    def _known_site(cls, v: str) -> str:
        return normalize_site(v)

    @property
    def customer_label(self) -> str:
        return " / ".join(self.customer_names)

    @property
    def shipment_ids(self) -> list[str]:
        return [s.shipment_id for s in self.shipments]

    @property
    def full_address(self) -> str:
        return f"{self.address}, {self.city}, {self.state} {self.zip}"

    @property
    def net_volume(self) -> float:
        """Volume change on the truck after the stop: negative = truck got emptier."""
        return self.pickup_volume - self.delivery_volume


class TruckConfig(BaseModel):
    """Physical truck and its assignable driver (Trucks & Drivers table)."""

    truck_number: str
    driver_name: str = ""
    active: bool = True


# --------------------------------------------------------------------------------------
# Plan side
# --------------------------------------------------------------------------------------


class LunchInfo(BaseModel):
    """Where a driver's lunch is taken.

    ``kind``: ``reload`` (during a reload between two loads), ``break`` (an explicit gap in
    a route), ``none`` (truck unused).  For ``break``, ``load_seq`` and ``after_stop_seq``
    locate the gap: ``after_stop_seq=0`` means before the first stop (right after leaving
    the hub); ``after_stop_seq=n`` means on the way from stop n to the next stop / hub.
    """

    kind: Literal["reload", "break", "none"] = "none"
    where: str = ""
    load_seq: int | None = None
    after_stop_seq: int | None = None
    start_s: int | None = None
    end_s: int | None = None


class StopVisit(BaseModel):
    seq: int
    stop_id: str
    arrive_s: int
    depart_s: int
    travel_min_from_prev: float
    dist_mi_from_prev: float
    volume_on_arrival: float
    volume_after: float
    weight_on_arrival: float = 0.0
    weight_after: float = 0.0
    pieces_on_arrival: int = 0
    pieces_after: int = 0
    eta_window_start_s: int
    eta_window_end_s: int
    lunch_before: bool = False  # lunch taken on the way to this stop
    solver_arrive_s: int | None = None  # solver's own cumul (for drift checks)


class Load(BaseModel):
    seq: int
    depart_s: int
    return_s: int
    load_out_volume: float
    max_volume_on_road: float
    load_out_weight: float = 0.0
    load_out_pieces: int = 0
    drive_minutes: float = 0.0
    distance_miles: float = 0.0
    reload_start_s: int | None = None  # for seq >= 2: when the reload started at the hub
    lunch_on_return: bool = False  # lunch taken after the last stop / at the hub
    stops: list[StopVisit] = Field(default_factory=list)
    solver_return_s: int | None = None
    site: str = DEFAULT_SITE
    pallet: bool = False
    deadhead_minutes: float = 0.0  # DC-to-DC drive before this load (turn at another site)
    one_way_miles: float = 0.0  # DC to the last stop
    last_delivery_s: int | None = None  # departure from the last stop

    @field_validator("site")
    @classmethod
    def _known_site(cls, v: str) -> str:
        return normalize_site(v)

    @property
    def stop_ids(self) -> list[str]:
        return [v.stop_id for v in self.stops]


class PlanTruck(BaseModel):
    number: str
    driver_name: str = ""
    on_duty_minutes: float = 0.0
    lunch: LunchInfo = Field(default_factory=LunchInfo)
    loads: list[Load] = Field(default_factory=list)

    @property
    def used(self) -> bool:
        return any(load.stops for load in self.loads)


class UnassignedStop(BaseModel):
    stop_id: str
    tier: int
    day_tag: DayTag
    volume: float
    pickup_volume: float = 0.0
    service_minutes: int = 0
    reason: str = ""
    detail: str = ""


class Violation(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    truck: str | None = None
    load: int | None = None
    stop_id: str | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience
        where = " ".join(
            p
            for p in (
                f"truck {self.truck}" if self.truck else "",
                f"load {self.load}" if self.load else "",
                f"stop {self.stop_id}" if self.stop_id else "",
            )
            if p
        )
        return (
            f"[{self.severity}] {where}: {self.message}"
            if where
            else f"[{self.severity}] {self.message}"
        )


class SolverStats(BaseModel):
    status: str = "not_run"
    status_code: int | None = None
    objective: int | None = None
    wall_time_s: float = 0.0
    vehicles_used: int = 0
    first_solution_found: bool = False
    first_solution_strategy: str = ""
    local_search: str = ""
    nodes: int = 0
    virtual_vehicles: int = 0
    lunch_mode: str = ""
    initial_solution: str = ""
    fleet_reduction: str = ""
    message: str = ""


class KPIs(BaseModel):
    trucks_used: int = 0
    trucks_selected: int = 0
    loads: int = 0
    stops_served: int = 0
    shipments_served: int = 0
    d_stops_total: int = 0
    d_stops_served: int = 0
    d_stops_served_pct: float = 0.0
    pull_forward_included: dict[str, int] = Field(default_factory=dict)
    pull_forward_available: dict[str, int] = Field(default_factory=dict)
    unassigned_by_tier: dict[str, int] = Field(default_factory=dict)
    unassigned_stops: int = 0
    unassigned_shipments: int = 0
    total_drive_minutes: float = 0.0
    total_miles: float = 0.0
    avg_on_duty_minutes: float = 0.0
    max_on_duty_minutes: float = 0.0
    fleet_volume_utilization_pct: float = 0.0
    load_utilization_pct: dict[str, float] = Field(default_factory=dict)
    avg_stops_per_load: float = 0.0
    demand_delivery_volume: float = 0.0
    available_volume: float = 0.0
    demand_service_minutes: float = 0.0
    available_minutes: float = 0.0


class Plan(BaseModel):
    """Output of one run: trucks -> loads -> ordered stop visits, plus unassigned stops."""

    run_id: str
    plan_version: int = 0
    plan_date: dt.date
    created_at: dt.datetime
    settings: dict = Field(default_factory=dict)  # RunSettings.model_dump(mode="json")
    matrix_source: str = "haversine"
    solver_stats: SolverStats = Field(default_factory=SolverStats)
    kpis: KPIs = Field(default_factory=KPIs)
    trucks: list[PlanTruck] = Field(default_factory=list)
    unassigned: list[UnassignedStop] = Field(default_factory=list)
    out_of_scope: list[UnassignedStop] = Field(
        default_factory=list
    )  # pull-forward days not enabled
    violations: list[Violation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    export_override_reason: str | None = None

    # ---- convenience -------------------------------------------------------------
    @property
    def used_trucks(self) -> list[PlanTruck]:
        return [t for t in self.trucks if t.used]

    @property
    def served_stop_ids(self) -> list[str]:
        return [v.stop_id for t in self.trucks for load in t.loads for v in load.stops]

    @property
    def hard_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    def truck(self, number: str) -> PlanTruck:
        for t in self.trucks:
            if t.number == number:
                return t
        raise KeyError(number)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def time_to_seconds(t: dt.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def seconds_to_time(s: int) -> dt.time:
    s = max(0, int(s)) % 86400
    return dt.time(s // 3600, (s % 3600) // 60, s % 60)


def clock(s: int | None, with_seconds: bool = False) -> str:
    """Render seconds-since-midnight as a local clock time like ``8:45 AM``."""
    if s is None:
        return ""
    day_offset, s = divmod(int(s), 86400)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    text = f"{h12}:{m:02d}:{sec:02d} {suffix}" if with_seconds else f"{h12}:{m:02d} {suffix}"
    return text + (" (+1d)" if day_offset else "")


def minutes(s: int | float) -> float:
    return round(float(s) / 60.0, 1)


def round_to_quarter(s: int, direction: Literal["nearest", "down", "up"] = "nearest") -> int:
    """Round seconds-since-midnight to the nearest 15 minutes."""
    q = 900
    if direction == "down":
        return (s // q) * q
    if direction == "up":
        return -((-s) // q) * q
    return int((s + q // 2) // q) * q
