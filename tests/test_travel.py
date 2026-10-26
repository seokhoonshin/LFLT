"""M2 tests: haversine provider, OSRM client (mocked), chunking, cache, provider selection."""

from __future__ import annotations

import json

import httpx
import pytest
from routeopt.config import AppConfig
from routeopt.domain import HUB_LAT, HUB_LON
from routeopt.travel import node_coords, select_provider
from routeopt.travel.base import coords_hash
from routeopt.travel.cache import CachedProvider, TravelCache
from routeopt.travel.haversine import HaversineProvider, haversine_m
from routeopt.travel.osrm import OSRMError, OSRMProvider

from tests.factories import make_settings, make_stops, point_at

HUB = (HUB_LAT, HUB_LON)


def test_haversine_matrix_scales_with_factor_and_speed() -> None:
    a = HUB
    b = point_at(10.0, 90.0)
    m = HaversineProvider(road_factor=1.3, avg_speed_mph=30).matrix([a, b])
    assert m.source == "haversine" and m.n == 2
    assert m.durations_s[0][0] == 0 and m.distances_m[1][1] == 0
    straight = haversine_m(a, b)
    assert m.distances_m[0][1] == pytest.approx(straight * 1.3, rel=0.01)
    # 10 miles * 1.3 at 30 mph = 26 minutes
    assert m.durations_s[0][1] == pytest.approx(26 * 60, rel=0.03)
    assert m.durations_s[0][1] == m.durations_s[1][0]
    assert HaversineProvider().route_geometry([a, b]) is None
    assert HaversineProvider().healthy()


def _osrm_handler(calls: list[httpx.Request], *, fail_first: int = 0, null_pair: bool = False):
    """Fake OSRM: durations = 60 s per index gap, distances = 1000 m per gap."""
    state = {"fails": fail_first}
    hav = HaversineProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if state["fails"] > 0:
            state["fails"] -= 1
            return httpx.Response(503, text="busy")
        if path.startswith("/nearest/"):
            return httpx.Response(200, json={"code": "Ok", "waypoints": []})
        if path.startswith("/table/"):
            coord_text = path.split("/table/v1/driving/")[1]
            coords = [tuple(map(float, c.split(","))) for c in coord_text.split(";")]
            n = len(coords)
            srcs = [int(s) for s in request.url.params.get("sources", "").split(";") if s]
            dsts = [int(s) for s in request.url.params.get("destinations", "").split(";") if s]
            srcs = srcs or list(range(n))
            dsts = dsts or list(range(n))
            durations = []
            distances = []
            for i in srcs:
                drow = []
                mrow = []
                for j in dsts:
                    a = (coords[i][1], coords[i][0])
                    b = (coords[j][1], coords[j][0])
                    d = hav.matrix([a, b]).durations_s[0][1] * 1.0
                    mtr = haversine_m(a, b) * 1.3
                    if (
                        null_pair
                        and i != j
                        and abs(coords[i][1] - coords[j][1]) < 1e-9
                        and abs(coords[i][0] - coords[j][0]) < 1e-9
                    ):
                        drow.append(None)
                        mrow.append(None)
                    else:
                        drow.append(d)
                        mrow.append(mtr)
                durations.append(drow)
                distances.append(mrow)
            return httpx.Response(
                200, json={"code": "Ok", "durations": durations, "distances": distances}
            )
        if path.startswith("/route/"):
            coord_text = path.split("/route/v1/driving/")[1]
            coords = [tuple(map(float, c.split(","))) for c in coord_text.split(";")]
            assert (
                request.url.params["geometries"] == "geojson"
                and request.url.params["overview"] == "full"
            )
            line = [[lon, lat] for lon, lat in coords]
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "routes": [{"geometry": {"type": "LineString", "coordinates": line}}],
                },
            )
        return httpx.Response(404, json={"code": "InvalidUrl"})

    return handler


def _provider(calls: list[httpx.Request], **kw) -> OSRMProvider:  # type: ignore[no-untyped-def]
    max_table = kw.pop("max_table_size", 2000)
    client = httpx.Client(transport=httpx.MockTransport(_osrm_handler(calls, **kw)))
    return OSRMProvider(
        "http://osrm.test:5000", client=client, max_table_size=max_table, backoff_s=0
    )


def test_osrm_matrix_single_request_and_health() -> None:
    calls: list[httpx.Request] = []
    p = _provider(calls)
    assert p.healthy()
    coords = node_coords(make_stops(5))
    m = p.matrix(coords)
    assert m.source == "osrm" and m.n == 6
    assert all(m.durations_s[i][i] == 0 for i in range(6))
    assert m.durations_s[0][1] > 0 and m.distances_m[0][1] > 0
    table_calls = [c for c in calls if c.url.path.startswith("/table/")]
    assert len(table_calls) == 1
    assert table_calls[0].url.params["annotations"] == "duration,distance"
    assert "sources" not in table_calls[0].url.params


def test_osrm_matrix_chunks_when_above_max_table_size() -> None:
    calls: list[httpx.Request] = []
    coords = node_coords(make_stops(9))  # 10 nodes
    full = _provider([]).matrix(coords)
    chunked = _provider(calls, max_table_size=6).matrix(coords)  # blocks of 3
    assert chunked.durations_s == full.durations_s
    assert chunked.distances_m == full.distances_m
    table_calls = [c for c in calls if c.url.path.startswith("/table/")]
    assert len(table_calls) == 16  # 4x4 blocks (ceil(10/3) = 4)
    assert all(len(c.url.path.split("/table/v1/driving/")[1].split(";")) <= 6 for c in table_calls)


def test_osrm_null_pairs_are_estimated() -> None:
    calls: list[httpx.Request] = []
    a = HUB
    coords = [a, point_at(5, 0), point_at(5, 0)]  # duplicate coordinates -> null pair
    m = _provider(calls, null_pair=True).matrix(coords)
    assert m.durations_s[1][2] == 0 and m.notes and "unroutable" in m.notes[0]


def test_osrm_retries_then_succeeds_and_fails_cleanly() -> None:
    calls: list[httpx.Request] = []
    p = _provider(calls, fail_first=2)
    m = p.matrix([HUB, point_at(3, 45)])
    assert m.n == 2 and len(calls) == 3
    bad = _provider([], fail_first=10)
    with pytest.raises(OSRMError):
        bad.matrix([HUB, point_at(3, 45)])
    assert not bad.healthy()


def test_osrm_route_geometry_returns_lat_lon() -> None:
    p = _provider([])
    line = p.route_geometry([HUB, point_at(3, 45), HUB])
    assert line is not None and len(line) == 3
    assert line[0] == pytest.approx(HUB, abs=1e-5)
    assert p.route_geometry([HUB]) is None


def test_travel_cache_is_order_independent() -> None:
    cache = TravelCache()
    coords = node_coords(make_stops(4))
    m = HaversineProvider().matrix(coords)
    assert cache.get("haversine", coords) is None
    cache.put(m)
    hit = cache.get("haversine", coords)
    assert hit is not None and hit.durations_s == m.durations_s and hit.coords == m.coords
    permuted = [coords[i] for i in (3, 0, 4, 1, 2)]
    hit2 = cache.get("haversine", permuted)
    assert hit2 is not None
    for a, i in enumerate((3, 0, 4, 1, 2)):
        for b, j in enumerate((3, 0, 4, 1, 2)):
            assert hit2.durations_s[a][b] == m.durations_s[i][j]
    assert cache.get("osrm", coords) is None  # keyed by provider too
    assert coords_hash(coords) == coords_hash(permuted)
    assert cache.hits == 2 and cache.misses == 2


def test_cached_provider_calls_backend_once() -> None:
    calls: list[httpx.Request] = []
    cp = CachedProvider(_provider(calls), TravelCache())
    coords = node_coords(make_stops(3))
    cp.matrix(coords)
    cp.matrix(list(reversed(coords)))
    assert len([c for c in calls if c.url.path.startswith("/table/")]) == 1
    assert cp.last_from_cache and cp.name == "osrm"


def test_select_provider_auto_falls_back_and_forced_osrm_raises() -> None:
    app = AppConfig(OSRM_URL="http://osrm.test:5000", _env_file=None)  # type: ignore[call-arg]
    down = _provider([], fail_first=99)
    choice = select_provider(make_settings(travel_provider="auto"), app, osrm=down)
    assert choice.name == "haversine" and choice.fallback_used
    with pytest.raises(RuntimeError):
        select_provider(make_settings(travel_provider="osrm"), app, osrm=down)
    up = _provider([])
    choice2 = select_provider(
        make_settings(travel_provider="auto"), app, cache=TravelCache(), osrm=up
    )
    assert choice2.name == "osrm" and not choice2.fallback_used
    assert isinstance(choice2.provider, CachedProvider)
    forced = select_provider(make_settings(travel_provider="haversine"), app, osrm=up)
    assert forced.name == "haversine" and not forced.fallback_used
    assert json.dumps(choice2.reason)


def test_same_location_floor_only_between_distinct_stops_on_one_point() -> None:
    p = point_at(10, 0)
    m = HaversineProvider(1.3, 30.0).matrix([(HUB_LAT, HUB_LON), p, p, point_at(20, 90)])
    assert m.duration(1, 2) == 0
    floored = m.with_same_location_floor(600)
    assert floored.duration(1, 2) == 600 and floored.duration(2, 1) == 600
    assert floored.duration(1, 1) == 0  # same node
    assert floored.duration(1, 3) == m.duration(1, 3)  # far apart: unchanged
    assert floored.duration(0, 1) == m.duration(0, 1)  # hub untouched
    assert m.with_same_location_floor(0) is m
