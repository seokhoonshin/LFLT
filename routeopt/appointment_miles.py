"""Keep an appointment load tidy after it was edited: stops at the same address become one
stop, the stops are numbered 1..n again, and the 'Miles from prev' of every stop is worked
out again for the new order (the warehouse -> stop 1 -> stop 2 ...).

Pure functions over the appointment rows (dicts); the app injects the travel data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from routeopt.appointments import DROPPED, norm_address, stop_status
from routeopt.domain import SITE_NODE
from routeopt.travel.base import Coord, Matrix, TravelProvider

log = logging.getLogger(__name__)

CENSUS_ONELINE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

Ref = tuple[str, str, str]  # ("dc", code, "") or ("stop", stop_id, address)


def merge_and_renumber(load_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """One address = one stop: the stops of a load that share an address are joined into the
    earliest of them (keeping a real stop id over a hand-added one), then the stops are
    numbered 1..n in their order.  Returns the changes as {row id: {stop_seq, stop_id}}."""
    live = [r for r in load_rows if r["stop_seq"] < 900]
    first: dict[str, dict[str, Any]] = {}  # address key -> the earliest row at that address
    for r in sorted(live, key=lambda x: (x["stop_seq"], x["id"])):
        first.setdefault(norm_address(r["address"]) or f"#{r['stop_seq']}", r)
    target: dict[int, int] = {}
    stop_id_of: dict[str, str] = {}
    for key, r in first.items():
        same = [x for x in live if (norm_address(x["address"]) or f"#{x['stop_seq']}") == key]
        real = next((x["stop_id"] for x in same if not str(x["stop_id"]).startswith("M-")), None)
        stop_id_of[key] = real or r["stop_id"]
        for x in same:
            target[x["id"]] = r["stop_seq"]
    order = {seq: n for n, seq in enumerate(sorted(set(target.values())), start=1)}
    changes: dict[int, dict[str, Any]] = {}
    for r in live:
        key = norm_address(r["address"]) or f"#{r['stop_seq']}"
        values = {"stop_seq": order[target[r["id"]]], "stop_id": stop_id_of[key]}
        if values["stop_seq"] != r["stop_seq"] or values["stop_id"] != r["stop_id"]:
            changes[r["id"]] = values
    return changes


def chain_miles(
    load_rows: list[dict[str, Any]],
    miles_fn: Callable[[Ref, Ref], float | None],
) -> dict[int, float | None]:
    """'Miles from prev' for every driven stop of one load (rows of one stage, in their
    current stop order): the warehouse -> the first stop -> the next ...  Declined and
    cancelled stops are skipped (the truck does not go there).  {row id: miles}."""
    if not load_rows:
        return {}
    by_seq: dict[int, list[dict[str, Any]]] = {}
    for r in load_rows:
        if r["stop_seq"] < 900:
            by_seq.setdefault(r["stop_seq"], []).append(r)
    prev: Ref = ("dc", load_rows[0]["dc"], "")
    out: dict[int, float | None] = {}
    for seq in sorted(by_seq):
        rows = by_seq[seq]
        if stop_status([r["status"] for r in rows]) in DROPPED:
            continue
        here: Ref = ("stop", str(rows[0]["stop_id"]), str(rows[0]["address"]))
        miles = miles_fn(prev, here)
        for r in rows:
            out[r["id"]] = miles
        prev = here
    return out


def census_locate(address: str, timeout_s: float = 10.0) -> Coord | None:
    """(lat, lon) of a one-line US address from the Census geocoder, or None."""
    try:
        resp = httpx.get(
            CENSUS_ONELINE_URL,
            params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        matches = resp.json()["result"]["addressMatches"]
        if not matches:
            return None
        c = matches[0]["coordinates"]
        return (float(c["y"]), float(c["x"]))
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        log.warning("Census lookup failed for %r: %s", address, exc)
        return None


class MilesCalculator:
    """Driving miles between the warehouse / stops of a run.  Stops the run knows use its
    travel matrix; a stop added by hand is located by its address (``locate``) and its miles
    come from ``provider`` (OSRM, else a straight-line estimate)."""

    def __init__(
        self,
        matrix: Matrix,
        node: dict[str, int],
        stop_addresses: dict[str, str],
        provider: TravelProvider,
        locate: Callable[[str], Coord | None] = census_locate,
    ) -> None:
        self.matrix = matrix
        self.node = node  # stop id -> node index in the matrix
        self.by_address = {norm_address(a): node[sid] for sid, a in stop_addresses.items()}
        self.provider = provider
        self.locate = locate
        self._located: dict[str, Coord | None] = {}
        self.unlocated: list[str] = []  # addresses that could not be found (for a warning)

    def _node(self, ref: Ref) -> int | None:
        if ref[0] == "dc":
            return SITE_NODE.get(ref[1])
        if ref[1] in self.node:
            return self.node[ref[1]]
        return self.by_address.get(norm_address(ref[2]))

    def _coord(self, ref: Ref) -> Coord | None:
        n = self._node(ref)
        if n is not None:
            return self.matrix.coords[n]
        key = norm_address(ref[2])
        if key not in self._located:
            self._located[key] = self.locate(ref[2]) if key else None
            if self._located[key] is None and ref[2] not in self.unlocated:
                self.unlocated.append(ref[2])
        return self._located[key]

    def miles(self, a: Ref, b: Ref) -> float | None:
        na, nb = self._node(a), self._node(b)
        if na is not None and nb is not None:
            return round(self.matrix.distance_miles(na, nb), 1)
        ca, cb = self._coord(a), self._coord(b)
        if ca is None or cb is None:
            return None
        if ca == cb:
            return 0.0
        try:
            return round(self.provider.matrix([ca, cb]).distance_miles(0, 1), 1)
        except Exception as exc:  # a travel service hiccup must not block a save
            log.warning("Miles lookup failed: %s", exc)
            return None
