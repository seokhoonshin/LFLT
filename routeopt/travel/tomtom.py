"""TomTom Matrix Routing v2: truck travel times with historical (predicted) traffic.

``POST {base}/routing/matrix/2?key=...`` with origins / destinations and options
``departAt`` (the delivery day at a representative departure time, local to the hub),
``traffic=historical``, ``travelMode=truck`` and the truck's size and weight.  Matrices
larger than ``max_cells`` (origins x destinations per request) are split into blocks.
Cells TomTom cannot route are filled from the fallback provider.  Drive times already
include traffic, so the OSRM traffic factor is not applied on top.

The provider's ``name`` doubles as the travel-cache key: ``tomtom:<hash>`` of the departure
time and truck, so a matrix is only reused for the same day, time and vehicle.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from routeopt.domain import HUB_TIMEZONE, hub_now
from routeopt.travel.base import Coord, Matrix, TravelProvider
from routeopt.travel.haversine import HaversineProvider

log = logging.getLogger(__name__)

TOMTOM_BASE_URL = "https://api.tomtom.com"
FT_TO_M = 0.3048
LB_TO_KG = 0.45359237


class TomTomError(RuntimeError):
    """Raised when TomTom cannot produce a matrix (bad key, quota, network)."""


@dataclass(frozen=True)
class TruckProfile:
    length_ft: float = 26.0
    height_ft: float = 13.5
    weight_lb: float = 26000.0

    def options(self) -> dict[str, Any]:
        return {
            "travelMode": "truck",
            "vehicleCommercial": True,
            "vehicleLength": round(self.length_ft * FT_TO_M, 2),
            "vehicleHeight": round(self.height_ft * FT_TO_M, 2),
            "vehicleWeight": round(self.weight_lb * LB_TO_KG),
        }


def departure(plan_date: dt.date, at: dt.time, now: dt.datetime | None = None) -> dt.datetime:
    """``plan_date`` at ``at`` in the hub's time zone.  TomTom only predicts the future, so a
    past date (e.g. re-planning a historical day) moves forward by whole weeks: same weekday,
    same time of day, so the same historical traffic pattern."""
    zone = ZoneInfo(HUB_TIMEZONE)
    when = dt.datetime.combine(plan_date, at, tzinfo=zone)
    now = now or hub_now().replace(tzinfo=zone)
    while when <= now:
        when += dt.timedelta(weeks=1)
    return when


def cache_name(when: dt.datetime, truck: TruckProfile) -> str:
    """Short, stable cache key (fits the 32-character matrix_source column)."""
    raw = f"{when.isoformat()}|{truck.length_ft}|{truck.height_ft}|{truck.weight_lb}"
    return "tomtom:" + hashlib.sha1(raw.encode()).hexdigest()[:10]


class TomTomProvider:
    """HTTP client for TomTom Matrix Routing v2 (synchronous endpoint, chunked)."""

    def __init__(
        self,
        api_key: str,
        depart_at: dt.datetime,
        truck: TruckProfile | None = None,
        base_url: str = TOMTOM_BASE_URL,
        client: httpx.Client | None = None,
        max_cells: int = 200,
        timeout_s: float = 60.0,
        retries: int = 3,
        backoff_s: float = 1.0,
        fallback: TravelProvider | None = None,
    ) -> None:
        if not api_key:
            raise TomTomError("No TomTom API key (set TOMTOM_API_KEY in .env).")
        self.api_key = api_key
        self.depart_at = depart_at
        self.truck = truck or TruckProfile()
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=5.0))
        self.max_cells = max(1, max_cells)
        self.retries = retries
        self.backoff_s = backoff_s
        self._fallback: TravelProvider = fallback or HaversineProvider()
        self.name = cache_name(depart_at, self.truck)
        self.requests_made = 0

    @property
    def label(self) -> str:
        t = self.truck
        return (
            f"TomTom truck times with historical traffic, departing "
            f"{self.depart_at.strftime('%a %Y-%m-%d %H:%M')} "
            f"({t.length_ft:g} ft, {t.height_ft:g} ft high, {t.weight_lb:,.0f} lb)"
        )

    def healthy(self) -> bool:
        """A key is configured (a real probe would spend quota)."""
        return bool(self.api_key)

    # ---- HTTP -----------------------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/routing/matrix/2"
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.requests_made += 1
                resp = self._client.post(url, params={"key": self.api_key}, json=body)
                if resp.status_code in (401, 403):
                    raise TomTomError(
                        f"TomTom refused the key ({resp.status_code}); check TOMTOM_API_KEY "
                        "and that Matrix Routing is enabled for it."
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"TomTom returned {resp.status_code}", request=resp.request, response=resp
                    )
                if resp.status_code != 200:
                    raise TomTomError(f"TomTom error {resp.status_code}: {resp.text[:300]}")
                return dict(resp.json())
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                log.warning(
                    "TomTom request failed (attempt %s/%s): %s", attempt + 1, self.retries, exc
                )
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff_s * (2**attempt))
        raise TomTomError(f"TomTom unavailable after {self.retries} attempts: {last}")

    def _block(
        self, coords: list[Coord], src: list[int], dst: list[int]
    ) -> dict[tuple[int, int], tuple[int, int]]:
        def point(i: int) -> dict[str, Any]:
            lat, lon = coords[i]
            return {"point": {"latitude": round(lat, 6), "longitude": round(lon, 6)}}

        body = {
            "origins": [point(i) for i in src],
            "destinations": [point(j) for j in dst],
            "options": {
                "departAt": self.depart_at.isoformat(timespec="seconds"),
                "routeType": "fastest",
                "traffic": "historical",
                **self.truck.options(),
            },
        }
        out: dict[tuple[int, int], tuple[int, int]] = {}
        for cell in self._post(body).get("data", []):
            summary = cell.get("routeSummary")
            if not summary:
                continue  # detailedError: unroutable, filled from the fallback
            i = src[int(cell["originIndex"])]
            j = dst[int(cell["destinationIndex"])]
            out[(i, j)] = (int(summary["travelTimeInSeconds"]), int(summary["lengthInMeters"]))
        return out

    # ---- matrix ---------------------------------------------------------------------
    def matrix(self, coords: list[Coord]) -> Matrix:
        n = len(coords)
        side = max(1, math.isqrt(self.max_cells))
        ranges = [list(range(s, min(s + side, n))) for s in range(0, n, side)]
        cells: dict[tuple[int, int], tuple[int, int]] = {}
        try:
            for src in ranges:
                for dst in ranges:
                    cells.update(self._block(coords, src, dst))
        except TomTomError as exc:  # bad key, quota, network: plan on the fallback instead
            log.error("TomTom matrix failed: %s", exc)
            fb = self._fallback.matrix(coords)
            fb.notes.insert(0, f"TomTom unavailable ({exc}); used {fb.source} travel times.")
            return fb
        est: Matrix | None = None
        missing = 0
        durations = [[0] * n for _ in range(n)]
        distances = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                got = cells.get((i, j))
                if got is None:
                    missing += 1
                    est = est or self._fallback.matrix(coords)
                    durations[i][j] = est.durations_s[i][j]
                    distances[i][j] = est.distances_m[i][j]
                else:
                    durations[i][j], distances[i][j] = got
        notes = [self.label + "."]
        if missing:
            notes.append(f"{missing} pairs TomTom could not route were estimated.")
            log.warning("TomTom: %s pairs filled from the fallback", missing)
        log.info("TomTom matrix %sx%s in %s request(s)", n, n, len(ranges) ** 2)
        return Matrix(
            source=self.name,
            coords=[(float(c[0]), float(c[1])) for c in coords],
            durations_s=durations,
            distances_m=distances,
            notes=notes,
        )

    def route_geometry(self, coords: list[Coord]) -> list[Coord] | None:
        """Map polylines come from the fallback (OSRM) - drawing only, no quota spent."""
        return self._fallback.route_geometry(coords)


def is_road_source(source: str) -> bool:
    """True for road-network travel times (OSRM or TomTom), False for straight-line."""
    return source == "osrm" or source.startswith("tomtom")


def source_label(source: str) -> str:
    if source == "osrm":
        return "OSRM road network"
    if source.startswith("tomtom"):
        return "TomTom truck times with historical traffic"
    return "ESTIMATED (straight-line)"
