"""Travel-time providers: OSRM (primary) and haversine (fallback), with caching."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from routeopt.config import AppConfig, RunSettings
from routeopt.domain import SITES, Stop
from routeopt.travel.base import Coord, Matrix, TravelProvider
from routeopt.travel.cache import CachedProvider, TravelCache
from routeopt.travel.haversine import HaversineProvider
from routeopt.travel.osrm import OSRMProvider

log = logging.getLogger(__name__)

FALLBACK_WARNING = "Travel times are ESTIMATED — OSRM was unavailable"


@dataclass
class ProviderChoice:
    provider: TravelProvider
    name: str
    fallback_used: bool
    reason: str = ""


def select_provider(
    settings: RunSettings,
    app: AppConfig | None = None,
    cache: TravelCache | None = None,
    osrm: OSRMProvider | None = None,
) -> ProviderChoice:
    """Pick OSRM or haversine per ``settings.travel_provider`` (auto = OSRM if healthy)."""
    app = app or AppConfig()
    haversine = HaversineProvider(settings.haversine_road_factor, settings.haversine_avg_speed_mph)
    choice: ProviderChoice
    if settings.travel_provider == "haversine":
        choice = ProviderChoice(haversine, "haversine", False, "Straight-line estimates chosen.")
    else:
        osrm = osrm or OSRMProvider(
            app.osrm_url,
            max_table_size=app.osrm_max_table_size,
            timeout_s=app.osrm_timeout_s,
            fallback=haversine,
        )
        if osrm.healthy():
            choice = ProviderChoice(osrm, "osrm", False, f"OSRM at {app.osrm_url} is healthy.")
        elif settings.travel_provider == "osrm":
            raise RuntimeError(
                f"OSRM at {app.osrm_url} is not responding and 'OSRM only' was selected. "
                "Start the OSRM container or switch the travel provider to Auto."
            )
        else:
            choice = ProviderChoice(
                haversine, "haversine", True, f"OSRM at {app.osrm_url} is not responding."
            )
    if cache is not None:
        choice.provider = CachedProvider(choice.provider, cache)
    return choice


def node_coords(stops: list[Stop]) -> list[Coord]:
    """Nodes 0..N_DEPOTS-1 = loading sites (every site, used or not, so node numbers never
    shift), then the stops in solver order."""
    return [(site.lat, site.lon) for site in SITES] + [(s.latitude, s.longitude) for s in stops]


__all__ = [
    "FALLBACK_WARNING",
    "CachedProvider",
    "HaversineProvider",
    "Matrix",
    "OSRMProvider",
    "ProviderChoice",
    "TravelCache",
    "TravelProvider",
    "node_coords",
    "select_provider",
]
