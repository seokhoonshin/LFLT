"""Row-level validation of the shipment workbook (brief 4.2).

Hard errors block the run and are reported as a table of row + problem written for a
planner ("Row 37: volume is missing").  Warnings are shown and the run proceeds.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from dateutil import parser as dateparser

from routeopt.config import RunSettings
from routeopt.domain import (
    DC_TO_SITE,
    HUB_LAT,
    HUB_LON,
    DayTag,
    OrderType,
    Shipment,
    ShipmentType,
    site_for_dc,
)
from routeopt.ingest.excel_loader import RawRow, excel_serial_to_date

Severity = Literal["error", "warning"]

_SUFFIX_RE = re.compile(r"_(LMD|RTN|INC)\s*$", re.IGNORECASE)
_PRODUCT_GROUP_RE = re.compile(r"[A-Z]{2,4}")
NO_PALLET_GROUP = "HA"  # CA LFLT: HA ships without a pallet; AC, ES, BS, MS, ... need one
_WS_RE = re.compile(r"\s+")


@dataclass
class RowIssue:
    row: int
    message: str
    severity: Severity = "error"
    shipment_id: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "Row": self.row,
            "Shipment": self.shipment_id or "",
            "Field": self.field or "",
            "Problem": self.message,
        }


@dataclass
class GeocodeQuery:
    key: str
    street: str
    city: str
    state: str
    zip: str


@dataclass
class GeocodeResult:
    key: str
    latitude: float | None
    longitude: float | None
    matched_address: str | None = None
    match_type: str | None = None
    from_cache: bool = False

    @property
    def matched(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class Geocoder(Protocol):
    """Anything that can turn address queries into coordinates (Census geocoder, fakes)."""

    def geocode(self, queries: list[GeocodeQuery]) -> dict[str, GeocodeResult]: ...


@dataclass
class ValidationResult:
    shipments: list[Shipment]
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)
    excluded_rows: list[RowIssue] = field(default_factory=list)
    geocoded_rows: list[int] = field(default_factory=list)
    geocode_failures: list[RowIssue] = field(default_factory=list)
    rows_read: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def issues_table(self, severity: Severity) -> list[dict[str, Any]]:
        items = self.errors if severity == "error" else self.warnings
        return [i.as_dict() for i in sorted(items, key=lambda i: i.row)]


# --------------------------------------------------------------------------------------
# Field parsers
# --------------------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip() == ""


def parse_str(value: Any) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _WS_RE.sub(" ", str(value)).strip()


def parse_float(value: Any) -> float | None:
    """Return a float or raise ValueError; None when blank."""
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "")
    return float(text)


def parse_int(value: Any) -> int | None:
    f = parse_float(value)
    if f is None:
        return None
    if abs(f - round(f)) > 1e-9:
        raise ValueError("not a whole number")
    return round(f)


def parse_date(value: Any) -> dt.date | None:
    if _is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        if 20000 < float(value) < 80000:
            return excel_serial_to_date(float(value))
        raise ValueError("not a date")
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return dateparser.parse(text, dayfirst=False).date()


def parse_zip(value: Any) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{1,5}", text):
        return text.zfill(5)
    if re.fullmatch(r"\d{1,5}-\d{4}", text):
        base, plus4 = text.split("-")
        return f"{base.zfill(5)}-{plus4}"
    return text


def zip5(zip_text: str) -> str:
    return zip_text.split("-")[0].strip()[:5]


def normalize_address_key(address: str, zip_text: str) -> str:
    """Stop consolidation key: upper-cased, whitespace-collapsed address + zip5."""
    addr = _WS_RE.sub(" ", address.upper().strip())
    addr = re.sub(r"[.,]+", "", addr)
    return f"{addr}|{zip5(zip_text)}"


def geocode_cache_key(address: str, city: str, state: str, zip_text: str) -> str:
    return "|".join(
        _WS_RE.sub(" ", part.upper().strip()) for part in (address, city, state, zip5(zip_text))
    )


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------------------
# Row validation
# --------------------------------------------------------------------------------------


def _parse_coord(value: Any, kind: str) -> float | None:
    f = parse_float(value)
    if f is None:
        return None
    limit = 90 if kind == "lat" else 180
    if abs(f) > limit or f == 0:
        raise ValueError(f"{kind} out of range")
    return f


def validate_rows(
    rows: list[RawRow],
    plan_date: dt.date,
    settings: RunSettings | None = None,
    geocoder: Geocoder | None = None,
    exclude_rows: set[int] | None = None,
    hub: tuple[float, float] = (HUB_LAT, HUB_LON),
) -> ValidationResult:
    """Validate raw rows, fill missing coordinates via ``geocoder`` and build Shipments."""
    settings = settings or RunSettings(plan_date=plan_date)
    exclude_rows = exclude_rows or set()
    errors: list[RowIssue] = []
    warnings: list[RowIssue] = []
    excluded: list[RowIssue] = []
    seen_ids: dict[str, int] = {}
    parsed: list[dict[str, Any]] = []

    for raw in rows:
        n = raw.row_number
        if n in exclude_rows:
            excluded.append(RowIssue(n, "Excluded by the planner.", "warning"))
            continue
        row_errors: list[RowIssue] = []

        def err(
            msg: str,
            fld: str | None = None,
            *,
            _n: int = n,
            _e: list[RowIssue] = row_errors,
            _sid: Any = None,
        ) -> None:
            _e.append(RowIssue(_n, msg, "error", field=fld))

        sid = parse_str(raw.get("shipment_id"))
        if not sid:
            err("Shipment ID is missing.", "shipment_id")
            errors.extend(row_errors)
            continue
        for e in row_errors:
            e.shipment_id = sid

        m = _SUFFIX_RE.search(sid)
        stype: ShipmentType | None = None
        if m:
            stype = ShipmentType(m.group(1).upper())
        elif settings.require_shipment_suffix:
            err("Shipment ID must end with _LMD, _RTN or _INC.", "shipment_id")
        else:
            stype = ShipmentType.LMD  # CA LFLT ids carry no suffix: every row is a delivery
        if sid.upper() in seen_ids:
            err(f"Duplicate shipment ID (also on row {seen_ids[sid.upper()]}).", "shipment_id")
        else:
            seen_ids[sid.upper()] = n

        otype: OrderType | None = None
        ot_text = parse_str(raw.get("order_type"))
        if not ot_text:
            err("Order type is missing (OBS, BUILDER or LTL).", "order_type")
        else:
            try:
                otype = OrderType(ot_text.upper())
            except ValueError:
                err(f"Unknown order type '{ot_text}' (use OBS, BUILDER or LTL).", "order_type")

        ddate: dt.date | None = None
        try:
            ddate = parse_date(raw.get("delivery_date"))
        except (ValueError, OverflowError, TypeError):
            err(f"Delivery date '{raw.get('delivery_date')}' is not a date.", "delivery_date")
        if ddate is None and not any(e.field == "delivery_date" for e in row_errors):
            err("Delivery date is missing.", "delivery_date")

        customer = parse_str(raw.get("customer_name"))
        if not customer:
            err("Customer name is missing.", "customer_name")
        address = parse_str(raw.get("address"))
        if not address:
            err("Address is missing.", "address")
        city = parse_str(raw.get("city"))
        if not city:
            err("City is missing.", "city")
        state = parse_str(raw.get("state"))
        if not state:
            err("State is missing.", "state")
        zip_text = parse_zip(raw.get("zip"))
        if not zip_text:
            err("ZIP is missing.", "zip")

        pieces: int | None = None
        try:
            pieces = parse_int(raw.get("pieces"))
        except ValueError:
            err(f"Pieces '{raw.get('pieces')}' is not a whole number.", "pieces")
        volume: float | None = None
        try:
            volume = parse_float(raw.get("volume_cuft"))
        except ValueError:
            err(f"Volume '{raw.get('volume_cuft')}' is not a number.", "volume_cuft")
        weight: float | None = None
        try:
            weight = parse_float(raw.get("weight_lb"))
        except ValueError:
            err(f"Weight '{raw.get('weight_lb')}' is not a number.", "weight_lb")
        service: int | None = None
        try:
            service = parse_int(raw.get("service_minutes"))
        except ValueError:
            err(
                f"Service minutes '{raw.get('service_minutes')}' is not a whole number.",
                "service_minutes",
            )
        computed_service = settings.unload_minutes_per_cuft is not None
        if service is None and computed_service:
            service = 0  # unloading is computed per stop from the volume
        if service is None and not any(e.field == "service_minutes" for e in row_errors):
            err("Service minutes is missing.", "service_minutes")
        elif service is not None and service < 0:
            err("Service minutes cannot be negative.", "service_minutes")

        model_code = parse_str(raw.get("model_code"))
        strict = settings.profile == "legacy"
        if not strict:  # CA LFLT files carry no model / weight / pieces detail
            pieces = 1 if pieces is None else pieces
            weight = 0.0 if weight is None else weight
            model_code = model_code or "-"

        origin_dc = parse_str(raw.get("origin_dc"))
        site = site_for_dc(origin_dc)
        if site is None:
            known = ", ".join(sorted(DC_TO_SITE))
            err(f"Unknown DC '{origin_dc}' (use {known}).", "origin_dc")
        pallet = False
        pallet_text = parse_str(raw.get("pallet_required"))
        if pallet_text is not None:
            flag = pallet_text.strip().upper()
            if flag in ("Y", "YES", "TRUE", "1"):
                pallet = True
            elif flag in ("N", "NO", "FALSE", "0"):
                pallet = False
            elif _PRODUCT_GROUP_RE.fullmatch(flag):
                # a product group code: HA ships without a pallet, every other group
                # (AC, ES, BS, MS, ...) needs one
                pallet = flag != NO_PALLET_GROUP
            else:
                err(
                    f"Pallet required '{pallet_text}' must be Y / N or a product group "
                    "(HA = no pallet, others = pallet).",
                    "pallet_required",
                )
        priority: int | None = None
        try:
            priority = parse_int(raw.get("priority"))
        except ValueError:
            err(f"Priority '{raw.get('priority')}' must be 1, 2 or 3.", "priority")
        if priority is not None and priority not in (1, 2, 3):
            err(f"Priority {priority} must be 1, 2 or 3.", "priority")
            priority = None
        notes = parse_str(raw.get("notes"))
        memo = parse_str(raw.get("memo"))
        if memo:
            notes = f"{notes} | {memo}" if notes else memo

        def has_error(fld: str, _e: list[RowIssue] = row_errors) -> bool:
            return any(e.field == fld for e in _e)

        if stype in (ShipmentType.LMD, ShipmentType.RTN):
            if pieces is None:
                if not has_error("pieces"):
                    err("Pieces is missing.", "pieces")
            elif pieces < 1:
                err("Pieces must be at least 1 for a delivery or return.", "pieces")
            if volume is None:
                if not has_error("volume_cuft"):
                    err("Volume is missing.", "volume_cuft")
            elif volume < 0:
                err("Volume cannot be negative.", "volume_cuft")
            elif settings.volume_exceeds(volume, pallet):
                cap = settings.load_cap_cuft(pallet)
                limit = f"less than {cap:g}" if settings.volume_strict else f"{cap:g}"
                err(
                    f"Volume {volume:g} cu ft is larger than one truck ({limit} cu ft"
                    + (" with a pallet" if pallet and cap != settings.truck_volume_cuft else "")
                    + ").",
                    "volume_cuft",
                )
            if weight is None:
                if not has_error("weight_lb"):
                    err("Weight is missing.", "weight_lb")
            elif weight < 0:
                err("Weight cannot be negative.", "weight_lb")
            if not model_code:
                err("Model code is missing.", "model_code")
        elif stype == ShipmentType.INC:
            if pieces is not None and pieces < 0:
                err("Pieces cannot be negative.", "pieces")
            pieces = pieces or 0
            if volume and volume > 0:
                warnings.append(
                    RowIssue(
                        n,
                        "Installation check has a volume; treated as 0 cu ft.",
                        "warning",
                        sid,
                        "volume_cuft",
                    )
                )
            if weight and weight > 0:
                warnings.append(
                    RowIssue(
                        n,
                        "Installation check has a weight; treated as 0 lb.",
                        "warning",
                        sid,
                        "weight_lb",
                    )
                )
            volume, weight = 0.0, 0.0

        if stype == ShipmentType.RTN and volume is not None and volume == 0:
            warnings.append(
                RowIssue(
                    n,
                    "Return pickup has 0 cu ft volume - nothing to pick up?",
                    "warning",
                    sid,
                    "volume_cuft",
                )
            )

        lat: float | None = None
        lon: float | None = None
        try:
            lat = _parse_coord(raw.get("latitude"), "lat")
            lon = _parse_coord(raw.get("longitude"), "lon")
        except ValueError:
            warnings.append(
                RowIssue(
                    n,
                    "Latitude/longitude not usable; will geocode the address.",
                    "warning",
                    sid,
                    "latitude",
                )
            )
            lat, lon = None, None
        if (lat is None) != (lon is None):
            lat, lon = None, None

        if row_errors:
            errors.extend(row_errors)
            continue

        assert ddate is not None and stype is not None and otype is not None
        offset = (ddate - plan_date).days
        if offset not in (0, 1, 2):
            excluded.append(
                RowIssue(
                    n,
                    f"Delivery date {ddate.isoformat()} is outside D..D+2 ({plan_date.isoformat()} .. "
                    f"{(plan_date + dt.timedelta(days=2)).isoformat()}); row excluded.",
                    "warning",
                    sid,
                    "delivery_date",
                )
            )
            continue

        parsed.append(
            {
                "shipment_id": sid,
                "shipment_type": stype,
                "purchase_order": parse_str(raw.get("purchase_order")),
                "delivery_date": ddate,
                "day_tag": DayTag.from_offset(offset),
                "order_type": otype,
                "customer_name": customer,
                "phone": parse_str(raw.get("phone")),
                "address": address,
                "city": city,
                "state": state.upper() if state else state,
                "zip": zip_text,
                "latitude": lat,
                "longitude": lon,
                "model_code": model_code,
                "pieces": pieces or 0,
                "weight_lb": float(weight or 0.0),
                "volume_cuft": float(volume or 0.0),
                "service_minutes": int(service or 0),
                "notes": notes,
                "source_row": n,
                "origin_dc": origin_dc.upper() if origin_dc else None,
                "site": site,
                "pallet_required": pallet,
                "priority": priority,
                "declared_value": float(parse_float(raw.get("declared_value")) or 0.0),
            }
        )

    warnings.extend(excluded)

    # ---- geocode fill -------------------------------------------------------------
    geocoded_rows: list[int] = []
    geocode_failures: list[RowIssue] = []
    missing = [p for p in parsed if p["latitude"] is None]
    if settings.geocode_mode == "address" and geocoder is not None:
        # every row is placed by its street address; file coordinates are only a fallback
        missing = list(parsed)
    if missing:
        if geocoder is None:
            for p in missing:
                geocode_failures.append(
                    RowIssue(
                        p["source_row"],
                        "Latitude/longitude missing and no geocoder available.",
                        "error",
                        p["shipment_id"],
                        "latitude",
                    )
                )
        else:
            queries: dict[str, GeocodeQuery] = {}
            for p in missing:
                key = geocode_cache_key(p["address"], p["city"], p["state"], p["zip"])
                queries.setdefault(
                    key, GeocodeQuery(key, p["address"], p["city"], p["state"], zip5(p["zip"]))
                )
            results = geocoder.geocode(list(queries.values()))
            for p in missing:
                key = geocode_cache_key(p["address"], p["city"], p["state"], p["zip"])
                res = results.get(key)
                if res and res.matched:
                    p["latitude"], p["longitude"] = res.latitude, res.longitude
                    geocoded_rows.append(p["source_row"])
                elif p["latitude"] is not None:
                    warnings.append(
                        RowIssue(
                            p["source_row"],
                            f"Address could not be geocoded: {p['address']}, {p['city']}, {p['state']} "
                            f"{p['zip']}; using the latitude/longitude from the file.",
                            "warning",
                            p["shipment_id"],
                            "address",
                        )
                    )
                else:
                    geocode_failures.append(
                        RowIssue(
                            p["source_row"],
                            f"Address could not be geocoded: {p['address']}, {p['city']}, {p['state']} {p['zip']}.",
                            "error",
                            p["shipment_id"],
                            "address",
                        )
                    )
    errors.extend(geocode_failures)

    shipments: list[Shipment] = []
    for p in parsed:
        if p["latitude"] is None:
            continue
        shipments.append(Shipment(**p))
        d = haversine_miles(hub[0], hub[1], p["latitude"], p["longitude"])
        if d > settings.coverage_radius_miles:
            warnings.append(
                RowIssue(
                    p["source_row"],
                    f"Address is {d:.0f} miles from the hub "
                    f"(coverage radius {settings.coverage_radius_miles:g} miles).",
                    "warning",
                    p["shipment_id"],
                    "address",
                )
            )

    return ValidationResult(
        shipments=shipments,
        errors=sorted(errors, key=lambda i: i.row),
        warnings=sorted(warnings, key=lambda i: i.row),
        excluded_rows=excluded,
        geocoded_rows=geocoded_rows,
        geocode_failures=geocode_failures,
        rows_read=len(rows),
    )
