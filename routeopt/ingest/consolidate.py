"""Consolidate shipments into stops (brief section 5).

Key: upper-cased, whitespace-collapsed ``address`` + ``zip``.  A stop is served entirely by
one load or not at all.  Stop attributes are computed once here and reused by the solver,
validator, editor and exports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from routeopt.config import RunSettings
from routeopt.domain import DEFAULT_SITE, DayTag, OrderType, Shipment, ShipmentType, Stop
from routeopt.ingest.validator import RowIssue, haversine_miles, normalize_address_key

NEAR_DUPLICATE_METERS = 10.0
LONG_SERVICE_WARNING_MINUTES = 240


@dataclass
class ConsolidationResult:
    stops: list[Stop]
    warnings: list[RowIssue] = field(default_factory=list)
    shipment_count: int = 0

    @property
    def summary(self) -> str:
        return f"{self.shipment_count} shipments -> {len(self.stops)} stops"

    @property
    def counts_by_day(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.stops:
            out[s.day_tag.value] = out.get(s.day_tag.value, 0) + 1
        return out

    @property
    def counts_by_tier(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for s in self.stops:
            out[s.tier] = out.get(s.tier, 0) + 1
        return dict(sorted(out.items()))


def stop_tier(day_tag: DayTag, order_types: list[OrderType]) -> int:
    """Tier per brief 7.4: D stops by best order type (OBS > BUILDER > LTL); D+1 = 4; D+2 = 5."""
    if day_tag == DayTag.D1:
        return 4
    if day_tag == DayTag.D2:
        return 5
    if OrderType.OBS in order_types:
        return 1
    if OrderType.BUILDER in order_types:
        return 2
    return 3


_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NAME_RE = re.compile(
    r"(?:call|contact|ask for|attn:?)\s+([A-Z][a-zA-Z'-]+(?:\s[A-Z][a-zA-Z'-]+)?)"
)
LOOK_UP_CONTACT = "Look up the official contact by address"


def appointment_contact(texts: list[str]) -> str:
    """Delivery appointment contact: a phone / e-mail written in Memo Text or notes (with the
    name next to it when there is one), otherwise look the official contact up by address."""
    for text in texts:
        found = _PHONE_RE.search(text) or _EMAIL_RE.search(text)
        if not found:
            continue
        name = _NAME_RE.search(text)
        return f"{name.group(1)} {found.group(0)}" if name else found.group(0)
    return LOOK_UP_CONTACT


def stop_key(shipment: Shipment) -> str:
    """Consolidation key: address + ZIP, per loading site (one address served from LGC2 and
    from LGC8 is two stops; LGC2 and LGC7 are one site)."""
    key = normalize_address_key(shipment.address, shipment.zip)
    return key if shipment.site == DEFAULT_SITE else f"{key}|{shipment.site}"


def build_stop(stop_id: str, key: str, shipments: list[Shipment], settings: RunSettings) -> Stop:
    """Compute all stop attributes from its shipments."""
    first = shipments[0]
    lats = [s.latitude for s in shipments if s.latitude is not None]
    lons = [s.longitude for s in shipments if s.longitude is not None]
    order_types: list[OrderType] = []
    for s in shipments:
        if s.order_type not in order_types:
            order_types.append(s.order_type)
    customer_names: list[str] = []
    for s in shipments:
        if s.customer_name not in customer_names:
            customer_names.append(s.customer_name)
    phones: list[str] = []
    for s in shipments:
        if s.phone and s.phone not in phones:
            phones.append(s.phone)
    pos: list[str] = []
    for s in shipments:
        if s.purchase_order and s.purchase_order not in pos:
            pos.append(s.purchase_order)
    day_tag = min((s.day_tag for s in shipments), key=lambda d: d.offset)
    if settings.unload_minutes_per_cuft is not None:
        moved = sum(s.delivery_volume + s.pickup_volume for s in shipments)
        unload = round(moved * settings.unload_minutes_per_cuft)
    else:
        unload = sum(s.service_minutes for s in shipments)
    service_total = unload + settings.stop_buffer_minutes
    priorities = [s.priority for s in shipments if s.priority is not None]
    tier = stop_tier(day_tag, order_types)
    if priorities and day_tag == DayTag.D:
        tier = min(priorities)  # CA: priority 1 existing LFLT, 2 add-on, 3 low
    return Stop(
        stop_id=stop_id,
        key=key,
        address=first.address,
        city=first.city,
        state=first.state,
        zip=first.zip,
        latitude=sum(lats) / len(lats),
        longitude=sum(lons) / len(lons),
        customer_names=customer_names,
        phones=phones,
        shipments=list(shipments),
        purchase_orders=pos,
        order_types=order_types,
        delivery_volume=sum(s.delivery_volume for s in shipments),
        pickup_volume=sum(s.pickup_volume for s in shipments),
        delivery_weight=sum(s.delivery_weight for s in shipments),
        pickup_weight=sum(s.pickup_weight for s in shipments),
        pieces_in=sum(s.pieces for s in shipments if s.shipment_type == ShipmentType.LMD),
        pieces_out=sum(s.pieces for s in shipments if s.shipment_type == ShipmentType.RTN),
        service_total_minutes=service_total,
        day_tag=day_tag,
        tier=tier,
        site=first.site,
        pallet=any(s.pallet_required for s in shipments),
        contact=appointment_contact([s.notes for s in shipments if s.notes]),
    )


def consolidate(shipments: list[Shipment], settings: RunSettings) -> ConsolidationResult:
    """Group shipments by address key into stops, in first-seen order, ids S001.."""
    groups: dict[str, list[Shipment]] = {}
    for s in shipments:
        groups.setdefault(stop_key(s), []).append(s)
    stops: list[Stop] = []
    warnings: list[RowIssue] = []
    for i, (key, group) in enumerate(groups.items(), start=1):
        stop = build_stop(f"S{i:03d}", key, group, settings)
        stops.append(stop)
        if stop.service_total_minutes > LONG_SERVICE_WARNING_MINUTES:
            warnings.append(
                RowIssue(
                    group[0].source_row,
                    f"Stop at {stop.address} needs {stop.service_total_minutes} minutes of service "
                    f"(more than {LONG_SERVICE_WARNING_MINUTES}).",
                    "warning",
                    group[0].shipment_id,
                    "service_minutes",
                )
            )
    # near-duplicate coordinates with different keys
    limit_miles = NEAR_DUPLICATE_METERS / 1609.344
    for a_idx in range(len(stops)):
        a = stops[a_idx]
        for b_idx in range(a_idx + 1, len(stops)):
            b = stops[b_idx]
            if abs(a.latitude - b.latitude) > 0.001 or abs(a.longitude - b.longitude) > 0.001:
                continue
            if haversine_miles(a.latitude, a.longitude, b.latitude, b.longitude) <= limit_miles:
                warnings.append(
                    RowIssue(
                        b.shipments[0].source_row,
                        f"'{b.address}' has the same coordinates as '{a.address}' but a different "
                        "address text; they are planned as separate stops.",
                        "warning",
                        b.shipments[0].shipment_id,
                        "address",
                    )
                )
    return ConsolidationResult(stops=stops, warnings=warnings, shipment_count=len(shipments))


def stops_by_id(stops: list[Stop]) -> dict[str, Stop]:
    return {s.stop_id: s for s in stops}
