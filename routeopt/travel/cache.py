"""Travel-matrix cache keyed by (provider, hash of the sorted coordinate list).

The stored matrix is in *sorted-coordinate* order so any request over the same set of
points (any order) is a hit; the matrix is permuted back to the requested order on read.
Reused across runs and across plan versions in the editor.
"""

from __future__ import annotations

import logging
from typing import Protocol

from routeopt.travel.base import (
    Coord,
    Matrix,
    TravelProvider,
    coords_hash,
    round_coord,
    sorted_unique_coords,
)

log = logging.getLogger(__name__)


class TravelCacheStore(Protocol):
    """Persistence backend for cached matrices (memory or database)."""

    def load(self, provider: str, key: str) -> Matrix | None: ...

    def save(self, provider: str, key: str, matrix: Matrix) -> None: ...


class MemoryTravelCacheStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Matrix] = {}

    def load(self, provider: str, key: str) -> Matrix | None:
        return self._data.get((provider, key))

    def save(self, provider: str, key: str, matrix: Matrix) -> None:
        self._data[(provider, key)] = matrix


class TravelCache:
    """Order-independent matrix cache in front of a store."""

    def __init__(self, store: TravelCacheStore | None = None) -> None:
        self.store = store or MemoryTravelCacheStore()
        self.hits = 0
        self.misses = 0

    def get(self, provider: str, coords: list[Coord]) -> Matrix | None:
        key = coords_hash(coords)
        canonical = self.store.load(provider, key)
        if canonical is None:
            self.misses += 1
            return None
        self.hits += 1
        position = {c: i for i, c in enumerate(canonical.coords)}
        index = [position[round_coord(c)] for c in coords]
        out = canonical.submatrix(index)
        out.coords = [(float(c[0]), float(c[1])) for c in coords]
        return out

    def put(self, matrix: Matrix) -> None:
        key = coords_hash(matrix.coords)
        canonical_coords = sorted_unique_coords(matrix.coords)
        first_index: dict[Coord, int] = {}
        for i, c in enumerate(matrix.coords):
            first_index.setdefault(round_coord(c), i)
        index = [first_index[c] for c in canonical_coords]
        canonical = matrix.submatrix(index)
        canonical.coords = canonical_coords
        self.store.save(matrix.source, key, canonical)


class CachedProvider:
    """Wraps a provider so matrices are served from the cache when available."""

    def __init__(self, provider: TravelProvider, cache: TravelCache) -> None:
        self.provider = provider
        self.cache = cache
        self.name = provider.name
        self.last_from_cache = False

    def matrix(self, coords: list[Coord]) -> Matrix:
        cached = self.cache.get(self.provider.name, coords)
        if cached is not None:
            self.last_from_cache = True
            return cached
        self.last_from_cache = False
        matrix = self.provider.matrix(coords)
        self.cache.put(matrix)  # keyed by the matrix's own source (a fallback's, if it fell back)
        return matrix

    def route_geometry(self, coords: list[Coord]) -> list[Coord] | None:
        return self.provider.route_geometry(coords)

    def healthy(self) -> bool:
        return self.provider.healthy()
