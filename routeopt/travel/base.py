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
