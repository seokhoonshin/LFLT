"""Fallback travel provider: great-circle distance x road factor at an average speed."""

from __future__ import annotations

import math

import numpy as np

from routeopt.travel.base import Coord, Matrix

EARTH_RADIUS_M = 6_371_008.8


def haversine_matrix_m(coords: list[Coord]) -> np.ndarray:
    """Pairwise great-circle distances in meters (float64 array)."""
    if not coords:
        return np.zeros((0, 0))
    lat = np.radians(np.array([c[0] for c in coords], dtype=float))
    lon = np.radians(np.array([c[1] for c in coords], dtype=float))
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat)[:, None] * np.cos(lat)[None, :] * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


class HaversineProvider:
    """Straight-line estimates; used when OSRM is unavailable or forced by the planner."""

    name = "haversine"

    def __init__(self, road_factor: float = 1.3, avg_speed_mph: float = 30.0) -> None:
        if road_factor <= 0 or avg_speed_mph <= 0:
            raise ValueError("road factor and average speed must be positive")
        self.road_factor = road_factor
        self.avg_speed_mph = avg_speed_mph

    def matrix(self, coords: list[Coord]) -> Matrix:
        great_circle = haversine_matrix_m(coords)
        road_m = great_circle * self.road_factor
        speed_m_s = self.avg_speed_mph * 1609.344 / 3600.0
        durations = np.rint(road_m / speed_m_s).astype(int)
        distances = np.rint(road_m).astype(int)
        np.fill_diagonal(durations, 0)
        np.fill_diagonal(distances, 0)
        return Matrix(
            source=self.name,
            coords=[(float(c[0]), float(c[1])) for c in coords],
            durations_s=durations.tolist(),
            distances_m=distances.tolist(),
            notes=[
                f"Straight-line estimate: distance x {self.road_factor:g} at "
                f"{self.avg_speed_mph:g} mph."
            ],
        )

    def route_geometry(self, coords: list[Coord]) -> list[Coord] | None:
        return None  # straight lines on the map

    def healthy(self) -> bool:
        return True


def haversine_m(a: Coord, b: Coord) -> float:
    """Single-pair great-circle distance in meters."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))
