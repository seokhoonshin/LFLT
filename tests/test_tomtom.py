"""TomTom Matrix Routing v2 provider (mocked HTTP): request shape, chunking, fallbacks."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx
from routeopt.config import AppConfig, RunSettings
from routeopt.domain import HUB_TIMEZONE
from routeopt.travel import node_coords, planning_matrix, select_provider
from routeopt.travel.haversine import HaversineProvider
from routeopt.travel.tomtom import TomTomProvider, TruckProfile, departure, is_road_source

from tests.factories import PLAN_DATE, make_stops

LA = ZoneInfo(HUB_TIMEZONE)
WHEN = dt.datetime(2030, 3, 6, 10, 0, tzinfo=LA)


def _server(calls: list[dict], *, status: int = 200, skip: set[tuple[int, int]] | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"params": dict(request.url.params), "body": body, "path": request.url.path})
        if status != 200:
            return httpx.Response(status, text="denied")
        data = []
        for a, o in enumerate(body["origins"]):
            for b, d in enumerate(body["destinations"]):
                if skip and (o["point"]["latitude"], d["point"]["latitude"]) in skip:
                    data.append({"originIndex": a, "destinationIndex": b, "detailedError": {}})
                    continue
                lat_gap = abs(o["point"]["latitude"] - d["point"]["latitude"])
                data.append(
                    {
                        "originIndex": a,
                        "destinationIndex": b,
                        "routeSummary": {
                            "lengthInMeters": round(lat_gap * 111_000) + 100,
                            "travelTimeInSeconds": round(lat_gap * 4000) + 60,
                        },
                    }
                )
        return httpx.Response(200, json={"data": data})

    return httpx.MockTransport(handler)


def _provider(calls: list[dict], **kw) -> TomTomProvider:  # type: ignore[no-untyped-def]
    transport = _server(calls, status=kw.pop("status", 200), skip=kw.pop("skip", None))
    return TomTomProvider(
        "KEY",
        WHEN,
        TruckProfile(26, 13.5, 26000),
        client=httpx.Client(transport=transport),
        backoff_s=0,
        **kw,
    )


def test_request_shape_and_chunking() -> None:
    calls: list[dict] = []
    coords = node_coords(make_stops(3))  # 3 depots + 3 stops = 6 nodes
    m = _provider(calls, max_cells=9).matrix(coords)  # blocks of 3 x 3
    assert len(calls) == 4 and m.n == 6 and m.source.startswith("tomtom:")
    assert all(m.durations_s[i][i] == 0 for i in range(6))
    assert m.durations_s[0][3] > 0 and m.distances_m[0][3] > 0
    first = calls[0]
    assert first["path"] == "/routing/matrix/2" and first["params"]["key"] == "KEY"
    opts = first["body"]["options"]
    assert opts["travelMode"] == "truck" and opts["traffic"] == "historical"
    assert opts["departAt"] == "2030-03-06T10:00:00-08:00"
    assert opts["vehicleLength"] == 7.92 and opts["vehicleWeight"] == 11793
    assert len(first["body"]["origins"]) * len(first["body"]["destinations"]) <= 9
    assert is_road_source(m.source) and "historical traffic" in m.notes[0]


def test_unroutable_cells_are_estimated() -> None:
    calls: list[dict] = []
    coords = node_coords(make_stops(2))
    lat = [round(c[0], 6) for c in coords]
    m = _provider(calls, skip={(lat[2], lat[3])}).matrix(coords)
    est = HaversineProvider().matrix(coords)
    assert m.durations_s[2][3] == est.durations_s[2][3]  # LGC8 -> first stop
    assert any("could not route" in n for n in m.notes)


def test_refused_key_falls_back_without_crashing() -> None:
    calls: list[dict] = []
    coords = node_coords(make_stops(2))
    m = _provider(calls, status=403).matrix(coords)
    assert m.source == "haversine" and "TomTom unavailable" in m.notes[0]
    assert len(calls) == 1  # a refused key is not retried


def test_departure_moves_a_past_date_forward_by_weeks() -> None:
    now = dt.datetime(2026, 10, 26, 9, 0, tzinfo=LA)
    past = departure(dt.date(2026, 8, 19), dt.time(10, 0), now=now)  # a Wednesday
    assert past.weekday() == 2 and past > now and past.time() == dt.time(10, 0)
    assert (past.date() - dt.date(2026, 8, 19)).days % 7 == 0
    future = departure(dt.date(2026, 10, 30), dt.time(10, 0), now=now)
    assert future == dt.datetime(2026, 10, 30, 10, 0, tzinfo=LA)


def test_select_provider_tomtom_without_key_falls_back() -> None:
    settings = RunSettings(plan_date=PLAN_DATE, travel_provider="tomtom")
    app = AppConfig(OSRM_URL="http://127.0.0.1:1", TOMTOM_API_KEY="")
    choice = select_provider(settings, app)
    assert choice.name == "haversine" and choice.fallback_used
    assert "No TomTom API key" in choice.reason
    keyed = select_provider(settings, AppConfig(OSRM_URL="http://127.0.0.1:1", TOMTOM_API_KEY="k"))
    assert keyed.name == "tomtom" and keyed.provider.name.startswith("tomtom:")


def test_traffic_factor_is_not_applied_to_tomtom_times() -> None:
    calls: list[dict] = []
    coords = node_coords(make_stops(2))
    m = _provider(calls).matrix(coords)
    planned = planning_matrix(m, RunSettings(plan_date=PLAN_DATE))  # CA: x 1.3 for OSRM only
    assert planned.durations_s == m.durations_s
