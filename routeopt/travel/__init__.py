"""Travel-time providers: OSRM (primary), TomTom (truck + traffic, optional) and haversine
(fallback), with caching."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from routeopt.config import AppConfig, RunSettings
from routeopt.domain import N_DEPOTS, SITES, Stop
from routeopt.travel.base import Coord, Matrix, TravelProvider
from routeopt.travel.cache import CachedProvider, TravelCache
from routeopt.travel.haversine import HaversineProvider
from routeopt.travel.osrm import OSRMProvider
from routeopt.travel.tomtom import TomTomProvider, TruckProfile, departure, is_road_source

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
    """Pick the travel-time source per ``settings.travel_provider``: auto = OSRM if healthy,
    else straight-line; tomtom = TomTom (OSRM / straight-line as its fallback)."""
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
        if settings.travel_provider == "tomtom":
            if not app.tomtom_api_key:
                choice.reason = "No TomTom API key (TOMTOM_API_KEY in .env); " + (
                    "using OSRM x traffic factor." if choice.name == "osrm" else choice.reason
                )
                choice.fallback_used = True
            else:
                tomtom = TomTomProvider(
                    app.tomtom_api_key,
                    departure(settings.plan_date, settings.traffic_depart_time),
                    TruckProfile(
                        settings.truck_length_ft,
                        settings.truck_height_ft,
                        settings.truck_gross_weight_lb,
                    ),
                    base_url=app.tomtom_base_url,
                    max_cells=app.tomtom_max_cells,
                    timeout_s=app.tomtom_timeout_s,
                    fallback=choice.provider,
                )
                choice = ProviderChoice(tomtom, "tomtom", False, tomtom.label + ".")
    if cache is not None:
        choice.provider = CachedProvider(choice.provider, cache)
    return choice


def planning_matrix(matrix: Matrix, settings: RunSettings) -> Matrix:
    """The times a plan is built and checked on: OSRM free-flow times x the traffic factor,
    and a floor between two different stops on the same point.  Applied on top of the raw
    provider matrix every time (the cache only ever holds raw matrices)."""
    if matrix.source == "osrm":
        matrix = matrix.with_traffic_factor(settings.traffic_factor)
    return matrix.with_same_location_floor(
        settings.same_location_travel_minutes * 60, first_stop=N_DEPOTS
    )


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
    "TomTomProvider",
    "TravelCache",
    "TravelProvider",
    "is_road_source",
    "node_coords",
    "planning_matrix",
    "select_provider",
]
