"""Travel-time provider interface and the Matrix container (brief section 6)."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

Coord = tuple[float, float]  # (latitude, longitude)


class Matrix(BaseModel):
    """Symmetric-shaped travel matrix over a coordinate list (node 0 is normally the hub)."""

    source: str
    coords: list[Coord]
    durations_s: list[list[int]]
    distances_m: list[list[int]]
    notes: list[str] = Field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.coords)

    def duration(self, i: int, j: int) -> int:
        return self.durations_s[i][j]

    def distance(self, i: int, j: int) -> int:
        return self.distances_m[i][j]

    def distance_miles(self, i: int, j: int) -> float:
        return self.distances_m[i][j] / 1609.344

    def with_traffic_factor(self, factor: float) -> Matrix:
        """Copy with every drive time multiplied by ``factor`` (distances unchanged)."""
        if factor == 1.0:
            return self
        durations = [[round(d * factor) for d in row] for row in self.durations_s]
        note = f"Road times x {factor:g} for traffic."
        return self.model_copy(update={"durations_s": durations, "notes": [*self.notes, note]})

    def with_same_location_floor(
        self, min_seconds: int, within_m: int = 160, first_stop: int = 1
    ) -> Matrix:
        """Copy where two different stops (nodes from ``first_stop`` on; the depots come
        first) less than ``within_m`` apart
        take at least ``min_seconds`` to drive between - e.g. two addresses both placed on
        one ZIP centroid, which the map would otherwise treat as 0 minutes apart."""
        if min_seconds <= 0:
            return self
        durations = [row[:] for row in self.durations_s]
        changed = 0
        for i in range(first_stop, self.n):
            for j in range(first_stop, self.n):
                if i != j and self.distances_m[i][j] < within_m and durations[i][j] < min_seconds:
                    durations[i][j] = min_seconds
                    changed += 1
        if not changed:
            return self
        note = (
            f"{changed // 2 or changed} stop pair(s) on the same point given at least "
            f"{min_seconds // 60} min of driving."
        )
        return self.model_copy(update={"durations_s": durations, "notes": [*self.notes, note]})

    def with_stop_transit(self, seconds: int, first_stop: int = 1) -> Matrix:
        """Copy where every drive between two different stops (nodes from ``first_stop`` on)
        takes ``seconds`` longer - the "stops in transit" allowance.  Drives from or back to
        a depot are unchanged."""
        if seconds <= 0:
            return self
        durations = [row[:] for row in self.durations_s]
        for i in range(first_stop, self.n):
            for j in range(first_stop, self.n):
                if i != j:
                    durations[i][j] += seconds
        note = f"{seconds // 60} min added to every drive between two stops (stops in transit)."
        return self.model_copy(update={"durations_s": durations, "notes": [*self.notes, note]})

    def submatrix(self, index: list[int]) -> Matrix:
        """Matrix restricted / re-ordered to the given node indices."""
        return Matrix(
            source=self.source,
            coords=[self.coords[i] for i in index],
            durations_s=[[self.durations_s[i][j] for j in index] for i in index],
            distances_m=[[self.distances_m[i][j] for j in index] for i in index],
            notes=list(self.notes),
        )


@runtime_checkable
class TravelProvider(Protocol):
    """Anything that can produce a travel matrix and (optionally) route geometry."""

    name: str

    def matrix(self, coords: list[Coord]) -> Matrix: ...

    def route_geometry(self, coords: list[Coord]) -> list[Coord] | None: ...

    def healthy(self) -> bool: ...


def round_coord(c: Coord, places: int = 5) -> Coord:
    return (round(float(c[0]), places), round(float(c[1]), places))


def coords_hash(coords: list[Coord]) -> str:
    """Hash of the *sorted* coordinate list rounded to 5 decimals (cache key, brief 6)."""
    normalized = sorted({round_coord(c) for c in coords})
    payload = json.dumps(normalized, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sorted_unique_coords(coords: list[Coord]) -> list[Coord]:
    return sorted({round_coord(c) for c in coords})
