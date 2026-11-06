"""OBS add-on (CA LFLT): OBS stops planned last, only near planned stops, or excluded."""

from __future__ import annotations

from routeopt.domain import OrderType, Stop
from routeopt.ingest.consolidate import consolidate
from routeopt.ingest.validator import haversine_miles
from routeopt.pipeline import default_trucks, optimize
from routeopt.solver.obs_addon import REASON_OBS_EXCLUDED, REASON_OBS_FAR, is_obs_stop
from routeopt.travel import node_coords
from routeopt.travel.haversine import HaversineProvider

from tests.factories import make_settings, make_shipment, point_at, renumber

SOLVE = 4


def _stops() -> list[Stop]:
    """Four LTL stops, an OBS stop 1 mi from each, and two OBS stops 40 mi away."""
    settings = make_settings()
    shipments = []
    for i, bearing in enumerate((0, 90, 180, 270)):
        lat, lon = point_at(12, bearing)
        shipments.append(
            make_shipment(
                address=f"{10 + i} Ltl Rd", lat=lat, lon=lon, order_type=OrderType.LTL, volume=60
            )
        )
        olat, olon = point_at(1, 45, lat, lon)
        shipments.append(
            make_shipment(address=f"{20 + i} Obs Ln", lat=olat, lon=olon, volume=5, service=5)
        )
    for i, bearing in enumerate((60, 240)):
        lat, lon = point_at(40, bearing)
        shipments.append(
            make_shipment(address=f"{30 + i} Far Way", lat=lat, lon=lon, volume=5, service=5)
        )
    return renumber(consolidate(shipments, settings).stops)


def _run(**kw: object):  # type: ignore[no-untyped-def]
    stops = _stops()
    settings = make_settings(solve_time_seconds=SOLVE, **kw)  # type: ignore[arg-type]
    matrix = HaversineProvider().matrix(node_coords(stops))
    return optimize(stops, matrix, settings, default_trucks()[:2]), stops


def test_obs_last_adds_only_near_planned_stops() -> None:
    res, stops = _run(obs_mode="last")
    plan = res.plan
    by_id = {s.stop_id: s for s in stops}
    served = set(plan.served_stop_ids)
    ltl = {s.stop_id for s in stops if not is_obs_stop(s)}
    assert ltl <= served  # OBS never displaces a planned stop
    near_obs = [s for s in stops if is_obs_stop(s) and s.address.endswith("Obs Ln")]
    assert {s.stop_id for s in near_obs} <= served
    far = {u.stop_id: u for u in plan.unassigned}
    assert {s.stop_id for s in stops if s.address.endswith("Far Way")} == set(far)
    assert all(u.reason == REASON_OBS_FAR for u in far.values())
    # every served OBS stop rides on a load with a planned stop within the radius
    for truck in plan.trucks:
        for load in truck.loads:
            planned = [by_id[v.stop_id] for v in load.stops if v.stop_id in ltl]
            for v in load.stops:
                if v.stop_id in ltl:
                    continue
                o = by_id[v.stop_id]
                assert any(
                    haversine_miles(o.latitude, o.longitude, p.latitude, p.longitude) <= 5
                    for p in planned
                )
    assert not plan.hard_violations
    assert plan.kpis.stops_served == len(ltl) + len(near_obs)
    assert any(w.startswith("OBS add-on: 4 of 6") for w in plan.warnings)


def test_obs_exclude_leaves_obs_out_of_scope() -> None:
    res, stops = _run(obs_mode="exclude")
    plan = res.plan
    obs = {s.stop_id for s in stops if is_obs_stop(s)}
    assert not obs & set(plan.served_stop_ids)
    assert {u.stop_id for u in plan.out_of_scope} == obs
    assert all(u.reason == REASON_OBS_EXCLUDED for u in plan.out_of_scope)
    assert not plan.unassigned and not plan.hard_violations
    assert res.in_scope_ids == {s.stop_id for s in stops} - obs


def test_obs_radius_controls_what_is_added() -> None:
    res, stops = _run(obs_mode="last", obs_radius_miles=0.5)
    obs = {s.stop_id for s in stops if is_obs_stop(s)}
    assert not obs & set(res.plan.served_stop_ids)
    assert all(u.reason == REASON_OBS_FAR for u in res.plan.unassigned)
