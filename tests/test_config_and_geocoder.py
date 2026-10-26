"""M1 tests: run settings, penalty invariants (acceptance criterion 9a), Census geocoder parsing."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from routeopt.config import (
    PRESET_A_FIXED_COST,
    AppConfig,
    RunSettings,
    validate_penalty_invariants,
)
from routeopt.domain import clock, round_to_quarter, seconds_to_time, time_to_seconds
from routeopt.ingest.census_geocoder import (
    CensusGeocoder,
    MemoryGeocodeCache,
    build_batch_csv,
    parse_batch_response,
)
from routeopt.ingest.validator import GeocodeQuery

from tests.factories import PLAN_DATE, make_settings


def test_defaults_match_brief() -> None:
    s = RunSettings(plan_date=PLAN_DATE)
    assert s.shift_start == dt.time(8, 0) and s.on_duty_hours == 10 and s.truck_volume_cuft == 600
    assert s.stop_buffer_minutes == 20 and s.reload_minutes == 60 and s.lunch_minutes == 30
    assert s.max_loads_per_truck == 3 and s.preset == "A" and s.solve_time_seconds == 60
    assert s.eta_window_hours == 2 and s.travel_provider == "auto"
    assert s.haversine_road_factor == 1.3 and s.haversine_avg_speed_mph == 30
    assert s.pull_forward_value_minutes == {1: 240, 2: 120}
    assert s.volume_cap_tenths == 6000 and s.on_duty_seconds == 36000
    assert (
        s.penalty_for_tier(1) == 100_000_000
        and s.penalty_for_tier(4) == 14_400
        and s.penalty_for_tier(5) == 7_200
    )
    assert (
        s.effective_truck_fixed_cost == PRESET_A_FIXED_COST
        and s.effective_span_cost_coefficient == 0
    )
    b = RunSettings(plan_date=PLAN_DATE, preset="B")
    assert b.effective_truck_fixed_cost == 0 and b.effective_span_cost_coefficient == 10


def test_settings_validation_messages() -> None:
    with pytest.raises(ValueError):
        RunSettings(plan_date=PLAN_DATE, solve_time_seconds=90)
    with pytest.raises(ValueError):
        RunSettings(plan_date=PLAN_DATE, pull_forward_days=[3])
    with pytest.raises(ValueError):
        RunSettings(plan_date=PLAN_DATE, lunch_earliest=dt.time(14), lunch_latest=dt.time(11))


def test_lunch_window_relative_to_shift() -> None:
    s = make_settings()
    assert s.lunch_window_rel() == (3 * 3600, 6 * 3600)
    s2 = make_settings(lunch_earliest=None, lunch_latest=None)
    assert s2.lunch_window_rel() == (0, s2.on_duty_seconds - s2.lunch_seconds)


def test_penalty_invariants_hold_for_presets() -> None:
    assert validate_penalty_invariants(make_settings(preset="A"), 10) == []
    assert validate_penalty_invariants(make_settings(preset="B"), 10) == []
    assert (
        validate_penalty_invariants(
            make_settings(preset="Custom", truck_fixed_cost=50_000, span_cost_coefficient=2), 10
        )
        == []
    )


def test_penalty_invariants_catch_bad_config() -> None:
    bad = make_settings(tier_penalties={1: 100, 2: 50, 3: 10})
    problems = validate_penalty_invariants(bad, 10)
    assert any("10x" in p for p in problems) and any("LTL (tier 3)" in p for p in problems)
    pf = make_settings(pull_forward_value_minutes={1: 100, 2: 500})
    problems = validate_penalty_invariants(pf, 10)
    assert any("Preset A fixed truck cost" in p for p in problems)
    assert any("D+1 pull-forward value must be higher" in p for p in problems)
    custom = make_settings(preset="Custom", truck_fixed_cost=1000, span_cost_coefficient=0)
    problems = validate_penalty_invariants(custom, 10)
    assert any("custom fixed truck cost" in p for p in problems)


def test_app_config_db_url(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ROUTEOPT_DB_URL", raising=False)
    cfg = AppConfig(ROUTEOPT_DATA_DIR=tmp_path, _env_file=None)  # type: ignore[call-arg]
    assert cfg.effective_db_url.startswith("sqlite:///")
    cfg2 = AppConfig(ROUTEOPT_DB_URL="postgresql://u:p@h/db", _env_file=None)  # type: ignore[call-arg]
    assert cfg2.effective_db_url == "postgresql://u:p@h/db"


def test_time_helpers() -> None:
    assert time_to_seconds(dt.time(8, 30)) == 30600
    assert seconds_to_time(30600) == dt.time(8, 30)
    assert (
        clock(30600) == "8:30 AM"
        and clock(13 * 3600 + 5 * 60) == "1:05 PM"
        and clock(0) == "12:00 AM"
    )
    assert round_to_quarter(8 * 3600 + 7 * 60) == 8 * 3600
    assert round_to_quarter(8 * 3600 + 8 * 60) == 8 * 3600 + 900


# ---- Census geocoder ----------------------------------------------------------------

BATCH_RESPONSE = (
    '"1","100 MAIN ST, RIALTO, CA, 92376","Match","Exact","100 MAIN ST, RIALTO, CA, 92376","-117.373,34.106","1234","L"\n'
    '"2","1 NOWHERE LN, RIALTO, CA, 92376","No_Match"\n'
    '"3","5 TIE RD, RIALTO, CA, 92376","Tie"\n'
)


def test_parse_batch_response_lon_lat_order() -> None:
    id_to_key = {"1": "k1", "2": "k2", "3": "k3"}
    res = parse_batch_response(BATCH_RESPONSE, id_to_key)
    assert res["k1"].matched and res["k1"].latitude == 34.106 and res["k1"].longitude == -117.373
    assert not res["k2"].matched and res["k2"].match_type == "No_Match"
    assert not res["k3"].matched


def test_build_batch_csv() -> None:
    q = GeocodeQuery("k", "100 Main St", "Rialto", "CA", "92376")
    assert build_batch_csv([q], {"k": "1"}) == b"1,100 Main St,Rialto,CA,92376\n"


def test_census_geocoder_uses_cache_and_mock_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.endswith("/addressbatch")
        body = request.read()
        assert b"benchmark" in body and b"Public_AR_Current" in body and b"addressFile" in body
        return httpx.Response(200, text=BATCH_RESPONSE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = MemoryGeocodeCache()
    geo = CensusGeocoder(cache=cache, client=client)
    queries = [
        GeocodeQuery("k1", "100 Main St", "Rialto", "CA", "92376"),
        GeocodeQuery("k2", "1 Nowhere Ln", "Rialto", "CA", "92376"),
        GeocodeQuery("k3", "5 Tie Rd", "Rialto", "CA", "92376"),
    ]
    res = geo.geocode(queries)
    assert res["k1"].matched and not res["k2"].matched and not res["k3"].matched
    assert len(seen) == 1
    # second call: k1 comes from cache, only unmatched ones are re-sent
    res2 = geo.geocode(queries[:1])
    assert res2["k1"].from_cache and len(seen) == 1


def test_census_geocoder_http_error_returns_unmatched(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("routeopt.ingest.census_geocoder.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    geo = CensusGeocoder(client=httpx.Client(transport=httpx.MockTransport(handler)), retries=1)
    res = geo.geocode([GeocodeQuery("k1", "100 Main St", "Rialto", "CA", "92376")])
    assert not res["k1"].matched and res["k1"].match_type == "Error"
