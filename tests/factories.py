"""Small factories shared by the test-suite (offline, deterministic)."""

from __future__ import annotations

import datetime as dt
import math
import random

from routeopt.config import RunSettings
from routeopt.domain import HUB_LAT, HUB_LON, DayTag, OrderType, Shipment, ShipmentType, Stop
from routeopt.ingest.consolidate import consolidate

PLAN_DATE = dt.date(2026, 9, 11)

# Test geography is expressed relative to the hub so a hub move never strands the suite.
NEAR_LAT = round(HUB_LAT + 0.0124, 4)  # ~1 mile north-east of the hub: default stop location
NEAR_LON = round(HUB_LON + 0.0100, 4)
BIG_SITE = (round(HUB_LAT + 0.0324, 4), round(HUB_LON + 0.0600, 4))  # builder site ~4 miles out
RETURN_SITE = (round(HUB_LAT - 0.0076, 4), round(HUB_LON + 0.0600, 4))  # return/INC address


def make_settings(**overrides: object) -> RunSettings:
    # obs_mode "normal": the legacy tests plan OBS stops as tier 1 like the brief
    base: dict[str, object] = {
        "plan_date": PLAN_DATE,
        "solve_time_seconds": 10,
        "obs_mode": "normal",
    }
    base.update(overrides)
    return RunSettings(**base)  # type: ignore[arg-type]


_counter = {"n": 0}


def make_shipment(
    stype: ShipmentType | str = ShipmentType.LMD,
    *,
    address: str = "100 Main St",
    zip_code: str = "92377",
    lat: float = NEAR_LAT,
    lon: float = NEAR_LON,
    volume: float = 40.0,
    weight: float = 150.0,
    pieces: int = 1,
    service: int = 30,
    order_type: OrderType | str = OrderType.OBS,
    day_offset: int = 0,
    po: str | None = None,
    customer: str = "Test Customer",
    sid: str | None = None,
    plan_date: dt.date = PLAN_DATE,
) -> Shipment:
    stype = ShipmentType(stype)
    order_type = OrderType(order_type)
    _counter["n"] += 1
    sid = sid or f"SH{_counter['n']:05d}_{stype.value}"
    if stype == ShipmentType.INC:
        volume, weight = 0.0, 0.0
    return Shipment(
        shipment_id=sid,
        shipment_type=stype,
        purchase_order=po,
        delivery_date=plan_date + dt.timedelta(days=day_offset),
        day_tag=DayTag.from_offset(day_offset),
        order_type=order_type,
        customer_name=customer,
        phone="(909) 555-0100",
        address=address,
        city="Rialto",
        state="CA",
        zip=zip_code,
        latitude=lat,
        longitude=lon,
        model_code="MODEL1" if stype != ShipmentType.INC else None,
        pieces=pieces,
        weight_lb=weight,
        volume_cuft=volume,
        service_minutes=service,
        notes=None,
        source_row=_counter["n"] + 1,
    )


def point_at(
    miles: float, bearing_deg: float, lat0: float = HUB_LAT, lon0: float = HUB_LON
) -> tuple[float, float]:
    """A point ``miles`` away from (lat0, lon0) along ``bearing_deg`` (flat-earth approximation)."""
    b = math.radians(bearing_deg)
    dlat = miles * math.cos(b) / 69.0
    dlon = miles * math.sin(b) / (69.0 * math.cos(math.radians(lat0)))
    return round(lat0 + dlat, 6), round(lon0 + dlon, 6)


def make_stops(
    n: int,
    *,
    settings: RunSettings | None = None,
    volume: float = 40.0,
    service: int = 30,
    order_type: OrderType | str = OrderType.OBS,
    day_offset: int = 0,
    max_miles: float = 25.0,
    seed: int = 1,
    prefix: str = "A",
) -> list[Stop]:
    """``n`` single-shipment stops scattered within ``max_miles`` of the hub."""
    settings = settings or make_settings()
    rng = random.Random(seed)
    shipments = []
    for i in range(n):
        miles = max_miles * math.sqrt(rng.random())
        lat, lon = point_at(miles, rng.random() * 360)
        shipments.append(
            make_shipment(
                address=f"{100 + i} {prefix} St",
                zip_code="92377",
                lat=lat,
                lon=lon,
                volume=volume,
                service=service,
                order_type=order_type,
                day_offset=day_offset,
                customer=f"Customer {prefix}{i}",
            )
        )
    return consolidate(shipments, settings).stops


def renumber(stops: list[Stop]) -> list[Stop]:
    """Give a combined stop list unique sequential ids."""
    return [s.model_copy(update={"stop_id": f"S{i + 1:03d}"}) for i, s in enumerate(stops)]
