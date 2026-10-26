"""US Census Geocoder (batch endpoint) used only when a row lacks lat/lon.

``POST https://geocoding.geo.census.gov/geocoder/locations/addressbatch`` with a CSV of
``id,street,city,state,zip`` and ``benchmark=Public_AR_Current``.  Free, no key, up to
10,000 rows per call.  Results are stored in a cache keyed by the normalized address so
repeat uploads do not re-call the service.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Protocol

import httpx

from routeopt.ingest.validator import GeocodeQuery, GeocodeResult

log = logging.getLogger(__name__)

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
DEFAULT_BENCHMARK = "Public_AR_Current"
MAX_BATCH_ROWS = 10_000


class GeocodeCache(Protocol):
    """Persistent store of geocoding results keyed by normalized address."""

    def get_many(self, keys: list[str]) -> dict[str, GeocodeResult]: ...

    def put_many(self, results: list[GeocodeResult]) -> None: ...


class MemoryGeocodeCache:
    """In-memory cache (tests, CLI without a database)."""

    def __init__(self) -> None:
        self._data: dict[str, GeocodeResult] = {}

    def get_many(self, keys: list[str]) -> dict[str, GeocodeResult]:
        return {k: self._data[k] for k in keys if k in self._data}

    def put_many(self, results: list[GeocodeResult]) -> None:
        for r in results:
            if r.matched:
                self._data[r.key] = r


def build_batch_csv(queries: list[GeocodeQuery], ids: dict[str, str]) -> bytes:
    """Build the ``id,street,city,state,zip`` CSV body (no header) for the batch API."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for q in queries:
        writer.writerow([ids[q.key], q.street, q.city, q.state, q.zip])
    return buf.getvalue().encode("utf-8")


def parse_batch_response(text: str, id_to_key: dict[str, str]) -> dict[str, GeocodeResult]:
    """Parse the batch CSV.  Coordinates come back as a single ``longitude,latitude`` field."""
    results: dict[str, GeocodeResult] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        rid = row[0].strip()
        key = id_to_key.get(rid)
        if key is None:
            continue
        indicator = row[2].strip() if len(row) > 2 else "No_Match"
        if indicator.lower() == "match" and len(row) >= 6 and row[5].strip():
            lon_text, lat_text = (part.strip() for part in row[5].split(","))
            try:
                lon, lat = float(lon_text), float(lat_text)
            except ValueError:
                results[key] = GeocodeResult(key, None, None, match_type=indicator)
                continue
            results[key] = GeocodeResult(
                key,
                latitude=lat,
                longitude=lon,
                matched_address=row[4].strip() if len(row) > 4 else None,
                match_type=row[3].strip() if len(row) > 3 else None,
            )
        else:
            results[key] = GeocodeResult(key, None, None, match_type=indicator)
    return results


class CensusGeocoder:
    """Batch geocoder with cache, retries and short timeouts."""

    def __init__(
        self,
        cache: GeocodeCache | None = None,
        client: httpx.Client | None = None,
        benchmark: str = DEFAULT_BENCHMARK,
        timeout_s: float = 60.0,
        retries: int = 2,
        url: str = CENSUS_BATCH_URL,
    ) -> None:
        self.cache = cache or MemoryGeocodeCache()
        self._client = client
        self.benchmark = benchmark
        self.timeout_s = timeout_s
        self.retries = retries
        self.url = url

    def geocode(self, queries: list[GeocodeQuery]) -> dict[str, GeocodeResult]:
        """Geocode all queries, serving cached keys locally and batching the rest."""
        unique: dict[str, GeocodeQuery] = {}
        for q in queries:
            unique.setdefault(q.key, q)
        results: dict[str, GeocodeResult] = {}
        cached = self.cache.get_many(list(unique))
        for key, res in cached.items():
            res.from_cache = True
            results[key] = res
        todo = [q for k, q in unique.items() if k not in results]
        if not todo:
            return results
        for start in range(0, len(todo), MAX_BATCH_ROWS):
            chunk = todo[start : start + MAX_BATCH_ROWS]
            results.update(self._geocode_chunk(chunk))
        self.cache.put_many([r for r in results.values() if r.matched and not r.from_cache])
        return results

    def _geocode_chunk(self, queries: list[GeocodeQuery]) -> dict[str, GeocodeResult]:
        ids = {q.key: str(i + 1) for i, q in enumerate(queries)}
        id_to_key = {v: k for k, v in ids.items()}
        body = build_batch_csv(queries, ids)
        client = self._client or httpx.Client(timeout=self.timeout_s)
        last_error: Exception | None = None
        try:
            for attempt in range(self.retries + 1):
                try:
                    resp = client.post(
                        self.url,
                        data={"benchmark": self.benchmark},
                        files={"addressFile": ("addresses.csv", body, "text/csv")},
                    )
                    resp.raise_for_status()
                    parsed = parse_batch_response(resp.text, id_to_key)
                    for key in ids:
                        parsed.setdefault(
                            key, GeocodeResult(key, None, None, match_type="No_Match")
                        )
                    return parsed
                except (httpx.HTTPError, ValueError) as exc:  # pragma: no cover - network
                    last_error = exc
                    log.warning("Census geocoder attempt %s failed: %s", attempt + 1, exc)
                    time.sleep(min(2**attempt, 8))
        finally:
            if self._client is None:
                client.close()
        log.error("Census geocoder failed after retries: %s", last_error)
        return {q.key: GeocodeResult(q.key, None, None, match_type="Error") for q in queries}
