"""OSRM travel provider (primary): table service for matrices, route service for geometry.

* ``GET {OSRM_URL}/table/v1/driving/{lon,lat;...}?annotations=duration,distance``
  Requests are chunked when the node count exceeds the server's ``max-table-size`` and the
  full matrix is assembled from the blocks.  Retries with backoff; short timeouts.
* ``GET {OSRM_URL}/route/v1/driving/...?overview=full&geometries=geojson`` for polylines.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from routeopt.domain import HUB_LAT, HUB_LON
from routeopt.travel.base import Coord, Matrix
from routeopt.travel.haversine import HaversineProvider

log = logging.getLogger(__name__)


class OSRMError(RuntimeError):
    """Raised when OSRM cannot produce a matrix after retries."""


def _fmt_coords(coords: list[Coord]) -> str:
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)


class OSRMProvider:
    """HTTP client for a local OSRM ``osrm-routed`` instance."""

    name = "osrm"

    def __init__(
        self,
        base_url: str = "http://localhost:5000",
        client: httpx.Client | None = None,
        max_table_size: int = 2000,
        timeout_s: float = 20.0,
        retries: int = 3,
        backoff_s: float = 0.5,
        fallback: HaversineProvider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=3.0))
        self.max_table_size = max(2, max_table_size)
        self.retries = retries
        self.backoff_s = backoff_s
        self._fallback = fallback or HaversineProvider()

    # ---- health ---------------------------------------------------------------------
    def healthy(self) -> bool:
        """True when the server answers a trivial ``nearest`` query with code Ok."""
        url = f"{self.base_url}/nearest/v1/driving/{HUB_LON:.6f},{HUB_LAT:.6f}"
        try:
            resp = self._client.get(url, params={"number": 1}, timeout=3.0)
            return resp.status_code == 200 and resp.json().get("code") == "Ok"
        except (httpx.HTTPError, ValueError):
            return False

    # ---- HTTP with retries ----------------------------------------------------------
    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"OSRM returned {resp.status_code}", request=resp.request, response=resp
                    )
                data = resp.json()
                if resp.status_code != 200 or data.get("code") != "Ok":
                    raise OSRMError(f"OSRM error: {data.get('code')} {data.get('message', '')}")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                log.warning(
                    "OSRM request failed (attempt %s/%s): %s", attempt + 1, self.retries, exc
                )
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff_s * (2**attempt))
        raise OSRMError(f"OSRM unavailable after {self.retries} attempts: {last}")

    # ---- table ----------------------------------------------------------------------
    def _table_block(
        self, coords: list[Coord], sources: list[int], destinations: list[int]
    ) -> tuple[list[list[float | None]], list[list[float | None]]]:
        url = f"{self.base_url}/table/v1/driving/{_fmt_coords(coords)}"
        params: dict[str, Any] = {"annotations": "duration,distance"}
        if len(sources) != len(coords):
            params["sources"] = ";".join(str(i) for i in sources)
        if len(destinations) != len(coords):
            params["destinations"] = ";".join(str(i) for i in destinations)
        data = self._get_json(url, params)
        return data["durations"], data["distances"]

    def matrix(self, coords: list[Coord]) -> Matrix:
        """Full duration/distance matrix, chunked to respect ``max_table_size``."""
        n = len(coords)
        durations: list[list[int | None]] = [[None] * n for _ in range(n)]
        distances: list[list[int | None]] = [[None] * n for _ in range(n)]
        if n <= self.max_table_size:
            d, m = self._table_block(coords, list(range(n)), list(range(n)))
            blocks = [(list(range(n)), list(range(n)), d, m)]
        else:
            blocks = []
            block = max(1, self.max_table_size // 2)
            ranges = [list(range(s, min(s + block, n))) for s in range(0, n, block)]
            for src in ranges:
                for dst in ranges:
                    if src == dst:
                        sub = [coords[i] for i in src]
                        d, m = self._table_block(sub, list(range(len(src))), list(range(len(src))))
                        blocks.append((src, src, d, m))
                    else:
                        sub = [coords[i] for i in src] + [coords[j] for j in dst]
                        s_idx = list(range(len(src)))
                        d_idx = list(range(len(src), len(src) + len(dst)))
                        d, m = self._table_block(sub, s_idx, d_idx)
                        blocks.append((src, dst, d, m))
        for src, dst, d, m in blocks:
            for a, i in enumerate(src):
                for b, j in enumerate(dst):
                    dv = d[a][b] if d and d[a] else None
                    mv = m[a][b] if m and m[a] else None
                    durations[i][j] = round(dv) if dv is not None else None
                    distances[i][j] = round(mv) if mv is not None else None
        notes: list[str] = []
        missing = sum(1 for i in range(n) for j in range(n) if durations[i][j] is None)
        if missing:
            est = self._fallback.matrix(coords)
            for i in range(n):
                for j in range(n):
                    if durations[i][j] is None:
                        durations[i][j] = est.durations_s[i][j]
                    if distances[i][j] is None:
                        distances[i][j] = est.distances_m[i][j]
            notes.append(f"{missing} unroutable pairs were estimated with straight lines.")
            log.warning("OSRM: %s unroutable pairs estimated with haversine", missing)
        for i in range(n):
            durations[i][i] = 0
            distances[i][i] = 0
        return Matrix(
            source=self.name,
            coords=[(float(c[0]), float(c[1])) for c in coords],
            durations_s=[[int(x) for x in row] for row in durations],  # type: ignore[arg-type]
            distances_m=[[int(x) for x in row] for row in distances],  # type: ignore[arg-type]
            notes=notes,
        )

    # ---- route geometry -------------------------------------------------------------
    def route_geometry(self, coords: list[Coord]) -> list[Coord] | None:
        """Road polyline through ``coords`` as (lat, lon) pairs, or None if unavailable."""
        if len(coords) < 2:
            return None
        url = f"{self.base_url}/route/v1/driving/{_fmt_coords(coords)}"
        try:
            data = self._get_json(url, {"overview": "full", "geometries": "geojson"})
        except OSRMError as exc:
            log.warning("OSRM route geometry unavailable: %s", exc)
            return None
        routes = data.get("routes") or []
        if not routes:
            return None
        line = routes[0].get("geometry", {}).get("coordinates") or []
        return [(float(lat), float(lon)) for lon, lat in line]
