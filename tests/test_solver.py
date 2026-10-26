"""M3/M4 tests: solver, validator, unassigned reasons, multi-load, lunch, pull-forward.

All offline (haversine), short solve times, property-based assertions (OR-Tools is not
perfectly deterministic under a time limit).
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import pytest
from routeopt.cli import format_plan
from routeopt.cli import main as cli_main
from routeopt.config import RunSettings
from routeopt.domain import DayTag, LunchInfo, Plan, Stop
from routeopt.ingest.consolidate import consolidate
from routeopt.pipeline import OptimizeJob, default_trucks, optimize, scope_stops
from routeopt.solver.extract import rebuild_plan_trucks
from routeopt.solver.plan_validator import check_truck, validate_plan
from routeopt.solver.timeline import RouteSkeleton, build_truck, node_index_map
from routeopt.solver.unassigned_reasons import (
    REASON_ANOMALY,
    REASON_TIME,
    REASON_VOLUME,
)
from routeopt.travel import node_coords
from routeopt.travel.base import Matrix
from routeopt.travel.haversine import HaversineProvider

from tests.factories import (
    BIG_SITE,
    RETURN_SITE,
    make_settings,
    make_shipment,
    make_stops,
    point_at,
    renumber,
)

SOLVE = 4  # seconds per solver test


def _matrix(stops: list[Stop]) -> Matrix:
    return HaversineProvider().matrix(node_coords(stops))


def _run(stops: list[Stop], trucks: int = 10, **kw: object):
    settings = make_settings(solve_time_seconds=SOLVE, **kw)  # type: ignore[arg-type]
    matrix = _matrix(stops)
    return optimize(stops, matrix, settings, default_trucks()[:trucks]), settings, matrix


def _served_by_tier(plan: Plan, stops: list[Stop]) -> Counter[int]:
    served = set(plan.served_stop_ids)
    return Counter(s.tier for s in stops if s.stop_id in served)


# ---- 2. basic feasibility -------------------------------------------------------------


def test_basic_feasibility_preset_a() -> None:
    stops = make_stops(8, volume=40, service=45)
    res, settings, matrix = _run(stops, preset="A")
    plan = res.plan
    assert plan.solver_stats.first_solution_found
    assert plan.kpis.stops_served == 8 and not plan.unassigned
    assert plan.violations == []
    assert plan.kpis.trucks_used <= 3
    assert validate_plan(plan, stops, settings, matrix) == []
    for truck in plan.used_trucks:
        for load in truck.loads:
            assert load.depart_s >= settings.shift_start_s
            assert load.return_s <= settings.shift_start_s + settings.on_duty_seconds
            assert load.stops[0].eta_window_start_s >= settings.shift_start_s
            assert all(
                v.eta_window_start_s % 900 == 0 and v.eta_window_end_s % 900 == 0
                for v in load.stops
            )
    text = format_plan(plan, stops)
    assert "Validator: no violations" in text and "Load 1" in text


# ---- 3. tier ordering under overflow --------------------------------------------------


def test_tier_ordering_under_overflow() -> None:
    obs = make_stops(10, volume=100, service=30, order_type="OBS", prefix="O", seed=3)
    bld = make_stops(8, volume=100, service=30, order_type="BUILDER", prefix="Q", seed=4)
    ltl = make_stops(8, volume=100, service=30, order_type="LTL", prefix="L", seed=5)
    stops = renumber(obs + bld + ltl)  # demand 2,600 cu ft vs 3 trucks x 600
    res, _settings, _m = _run(stops, trucks=3, max_loads_per_truck=1)
    plan = res.plan
    served = _served_by_tier(plan, stops)
    unassigned = Counter(u.tier for u in plan.unassigned)
    assert served[1] == 10, "every OBS stop must be served"
    assert unassigned.get(1, 0) == 0
    # all LTL dropped before any BUILDER, all BUILDER before any OBS
    if unassigned.get(2, 0):
        assert served[3] == 0
    if unassigned.get(1, 0):
        assert served[2] == 0 and served[3] == 0
    assert all(u.reason != REASON_ANOMALY for u in plan.unassigned)
    assert res.anomalies == []
    assert plan.violations == []


# ---- 4. single-stop full truck + second load ------------------------------------------


def test_full_truck_stop_rides_alone_and_truck_runs_second_load() -> None:
    settings = make_settings()
    big = make_shipment(
        address="1 Big Builder Rd",
        lat=BIG_SITE[0],
        lon=BIG_SITE[1],
        volume=600,
        service=120,
        order_type="BUILDER",
    )
    small = make_stops(3, volume=30, service=30, max_miles=8, prefix="B")
    stops = renumber(consolidate([big], settings).stops + small)
    res, settings, _matrix_unused = _run(stops, trucks=1)
    plan = res.plan
    assert plan.violations == [] and not plan.unassigned
    truck = plan.used_trucks[0]
    loads = [load for load in truck.loads if load.stops]
    assert len(loads) == 2
    big_load = next(load for load in loads if any(v.stop_id == "S001" for v in load.stops))
    assert len(big_load.stops) == 1 and big_load.load_out_volume == 600
    second = loads[1]
    assert second.reload_start_s is not None
    assert second.depart_s - second.reload_start_s == settings.reload_seconds
    assert second.reload_start_s == loads[0].return_s
    assert truck.on_duty_minutes <= settings.on_duty_hours * 60
    assert truck.lunch.kind == "reload"


# ---- 5. pickup ordering (VRPSPD) -----------------------------------------------------


def test_pickup_ordering_validator_and_solver() -> None:
    settings = make_settings()
    rtn = make_shipment(
        "RTN", address="9 Return Ln", lat=RETURN_SITE[0], lon=RETURN_SITE[1], volume=300, service=20
    )
    dels = make_stops(5, volume=100, service=30, max_miles=8, prefix="C")
    stops = renumber(consolidate([rtn], settings).stops + dels)
    matrix = _matrix(stops)
    by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    # a hand-built sequence that visits the return first: 500 loaded + 300 picked up = 800 > 600
    bad = RouteSkeleton("T01", "D", loads=[[s.stop_id for s in stops]])
    truck = build_truck(bad, by_id, matrix, node_of, settings)
    bad_plan = Plan(
        run_id="t",
        plan_date=settings.plan_date,
        created_at=__import__("datetime").datetime.now(),
        trucks=[truck],
    )
    problems = validate_plan(bad_plan, stops, settings, matrix)
    assert any(p.code == "volume" and p.severity == "error" for p in problems)
    # the solver's own answer is valid: the return is picked up only after enough deliveries
    res, _s, _m = _run(stops, trucks=1, max_loads_per_truck=1)
    plan = res.plan
    assert plan.violations == [] and not plan.unassigned
    load = plan.used_trucks[0].loads[0]
    assert load.load_out_volume == 500
    assert all(v.volume_after <= 600 for v in load.stops)
    assert max(v.volume_after for v in load.stops) <= 600


# ---- 6. on-duty limit ---------------------------------------------------------------


def test_on_duty_limit_far_flung_stops() -> None:
    base = make_stops(8, volume=20, service=150, seed=9, prefix="F")
    far = []
    for i, s in enumerate(base):
        lat, lon = point_at(35, i * 45)
        far.append(s.model_copy(update={"latitude": lat, "longitude": lon}))
    res, _settings, _m = _run(far, trucks=2, max_loads_per_truck=2)
    plan = res.plan
    assert plan.violations == []
    for truck in plan.used_trucks:
        assert truck.on_duty_minutes <= 600
    assert plan.unassigned, "not everything can fit in 10 h"
    assert all(u.reason == REASON_TIME for u in plan.unassigned)
    assert plan.kpis.stops_served >= 2


# ---- 7. lunch -----------------------------------------------------------------------


@pytest.mark.parametrize("lunch_mode", ["smart", "simple"])
def test_lunch_single_and_two_load_trucks(lunch_mode: str) -> None:
    settings = make_settings(solve_time_seconds=SOLVE, lunch_mode=lunch_mode)
    big = make_shipment(
        address="1 Big Builder Rd",
        lat=BIG_SITE[0],
        lon=BIG_SITE[1],
        volume=600,
        service=120,
        order_type="BUILDER",
    )
    small = make_stops(3, volume=30, service=30, max_miles=8, prefix="B")
    stops = renumber(consolidate([big], settings).stops + small)
    matrix = _matrix(stops)
    res = optimize(stops, matrix, settings, default_trucks()[:1])
    plan = res.plan
    assert [v for v in plan.violations if v.severity == "error"] == []
    truck = plan.used_trucks[0]
    assert len([load for load in truck.loads if load.stops]) == 2
    if lunch_mode == "smart":
        assert truck.lunch.kind == "reload"  # no explicit gap needed
    else:
        assert truck.lunch.kind == "break"  # simple mode always books a gap
    # a single-load truck must have a >= 30-min gap with no service
    single = make_stops(5, volume=40, service=45, seed=8, prefix="G")
    m2 = _matrix(single)
    res2 = optimize(single, m2, settings, default_trucks()[:1])
    plan2 = res2.plan
    assert [v for v in plan2.violations if v.severity == "error"] == []
    t2 = plan2.used_trucks[0]
    assert t2.lunch.kind == "break" and t2.lunch.start_s is not None and t2.lunch.end_s is not None
    assert t2.lunch.end_s - t2.lunch.start_s >= settings.lunch_seconds
    visits = [v for load in t2.loads for v in load.stops]
    assert not any(v.arrive_s < t2.lunch.end_s and t2.lunch.start_s < v.depart_s for v in visits)
    earliest, latest = settings.lunch_window_rel()
    rel = t2.lunch.start_s - settings.shift_start_s
    assert earliest <= rel <= latest


# ---- 8. pull-forward ----------------------------------------------------------------


def test_pull_forward_pools() -> None:
    d = make_stops(6, volume=60, service=40, seed=11, prefix="D")
    d1 = make_stops(4, volume=60, service=40, seed=12, prefix="E", day_offset=1)
    d2 = make_stops(4, volume=60, service=40, seed=13, prefix="G", day_offset=2)
    stops = renumber(d + d1 + d2)

    def served_by_day(plan: Plan) -> Counter[str]:
        served = set(plan.served_stop_ids)
        return Counter(s.day_tag.value for s in stops if s.stop_id in served)

    res_off, _s, _m = _run(stops, trucks=2, pull_forward_days=[])
    assert served_by_day(res_off.plan) == Counter({"D": 6})
    assert len(res_off.plan.out_of_scope) == 8 and not res_off.plan.unassigned

    res_on, _s, _m = _run(stops, trucks=2, pull_forward_days=[1, 2])
    by_day = served_by_day(res_on.plan)
    assert by_day["D"] == 6, "no D stop may be displaced"
    assert by_day["D+1"] > 0
    assert res_on.plan.violations == []
    # D+2 never ahead of feasible D+1: if any D+1 is unassigned, no D+2 may be served
    if any(u.day_tag == DayTag.D1 for u in res_on.plan.unassigned):
        assert by_day.get("D+2", 0) == 0

    res_d1, _s, _m = _run(stops, trucks=2, pull_forward_days=[1])
    assert served_by_day(res_d1.plan).get("D+2", 0) == 0
    assert res_d1.plan.kpis.pull_forward_included["D+1"] == served_by_day(res_d1.plan)["D+1"]


# ---- 9. a pull-forward never opens an extra truck under Preset A ----------------------


def test_pull_forward_never_opens_truck_preset_a() -> None:
    """One pull-forward stop is worth 240 min (14,400) < one truck (20,000): never opens one."""
    # 4 x (100 + 20) min service + ~50 min travel + 30 min lunch = ~560 min: exactly one truck
    d = make_stops(4, volume=50, service=100, max_miles=6, seed=14, prefix="D")
    d1 = make_stops(1, volume=50, service=60, max_miles=6, seed=15, prefix="E", day_offset=1)
    stops = renumber(d + d1)
    settings = make_settings(solve_time_seconds=8, preset="A", pull_forward_days=[1])
    res = optimize(stops, _matrix(stops), settings, default_trucks()[:3])
    plan = res.plan
    served = set(plan.served_stop_ids)
    assert all(s.stop_id in served for s in stops if s.day_tag == DayTag.D)
    assert plan.kpis.trucks_used == 1, "a pull-forward stop must not open a second truck"
    assert "closed" in plan.solver_stats.fleet_reduction or plan.solver_stats.fleet_reduction
    assert [u.tier for u in plan.unassigned] == [4]
    assert "Not worth it" in plan.unassigned[0].reason


# ---- 10. validator agreement ---------------------------------------------------------


def test_validator_matches_solver_times() -> None:
    stops = make_stops(25, volume=45, service=40, seed=17)
    res, _s, _m = _run(stops, trucks=6)
    plan = res.plan
    assert plan.solver_stats.first_solution_found
    drift = [v for v in plan.violations if v.code == "drift"]
    assert drift == []
    for truck in plan.used_trucks:
        for load in truck.loads:
            for visit in load.stops:
                assert visit.solver_arrive_s is not None


# ---- 12. solve time -----------------------------------------------------------------


def test_solve_time_150_stops() -> None:
    stops = make_stops(150, volume=45, service=50, seed=21, max_miles=35)
    settings = make_settings(solve_time_seconds=6)
    matrix = _matrix(stops)
    t0 = time.perf_counter()
    res = optimize(stops, matrix, settings, default_trucks())
    elapsed = time.perf_counter() - t0
    assert elapsed <= settings.solve_time_seconds + 5
    assert res.plan.solver_stats.first_solution_found
    assert [v for v in res.plan.violations if v.severity == "error"] == []


# ---- unassigned reasons and misc ---------------------------------------------------


def test_out_of_volume_reason() -> None:
    stops = make_stops(
        6, volume=300, service=20, max_miles=5, seed=30
    )  # 1,800 cu ft vs 1 truck x 1 load
    res, _s, _m = _run(stops, trucks=1, max_loads_per_truck=1)
    plan = res.plan
    assert plan.kpis.stops_served == 2
    assert all(u.reason == REASON_VOLUME for u in plan.unassigned)
    assert "cu ft" in plan.unassigned[0].detail


def test_scope_stops_restricts_matrix() -> None:
    stops = renumber(make_stops(2, prefix="D") + make_stops(2, prefix="E", day_offset=1))
    matrix = _matrix(stops)
    kept, sub, excluded = scope_stops(stops, matrix, make_settings(pull_forward_days=[]))
    assert [s.stop_id for s in kept] == ["S001", "S002"] and sub.n == 3
    assert [e.stop_id for e in excluded] == ["S003", "S004"]
    assert sub.durations_s[1][2] == matrix.durations_s[1][2]


def test_rebuild_plan_trucks_recomputes_after_structural_change() -> None:
    stops = make_stops(6, volume=40, service=30, seed=31)
    res, settings, matrix = _run(stops, trucks=2)
    plan = res.plan
    truck = plan.used_trucks[0]
    # drop the last stop of the first load -> it must show up as unassigned after rebuild
    load = truck.loads[0]
    removed = load.stops[-1].stop_id
    load.stops = load.stops[:-1]
    rebuilt = rebuild_plan_trucks(plan, stops, settings, matrix)
    assert removed in {u.stop_id for u in rebuilt.unassigned}
    assert validate_plan(rebuilt, stops, settings, matrix) == [] or all(
        v.severity == "warning" for v in validate_plan(rebuilt, stops, settings, matrix)
    )


def test_validator_flags_lunch_and_on_duty_problems() -> None:
    settings = make_settings()
    stops = make_stops(3, volume=40, service=200, seed=32, max_miles=30)
    matrix = _matrix(stops)
    by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    skel = RouteSkeleton(
        "T01", "D", loads=[[s.stop_id for s in stops]], lunch=LunchInfo(kind="none")
    )
    truck = build_truck(skel, by_id, matrix, node_of, settings)
    assert truck.lunch.kind == "break"  # 'none' is replaced by a rule-placed break
    # tamper: remove the lunch and stretch the day
    truck.lunch = LunchInfo(kind="none")
    plan = Plan(
        run_id="t",
        plan_date=settings.plan_date,
        created_at=__import__("datetime").datetime.now(),
        trucks=[truck],
    )
    codes = {v.code for v in validate_plan(plan, stops, settings, matrix)}
    assert "on_duty" in codes  # 3 x 220 min + travel > 600
    # the rule checks on the tampered truck itself flag the missing lunch
    assert "lunch" in {v.code for v in check_truck(truck, by_id, settings)}


def test_optimize_job_runs_in_background() -> None:
    stops = make_stops(5, volume=40, service=30, seed=33)
    settings = make_settings(solve_time_seconds=2)
    job = OptimizeJob(stops, _matrix(stops), settings, default_trucks()[:2])
    result = job.wait(timeout=90)
    assert job.done and job.error is None, job.error
    assert result is not None and result.plan.kpis.stops_served == 5
    assert job.elapsed >= 1.5


def test_cli_run_sample(
    sample_workbook: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from routeopt.sample_data import generate_sample

    from tests.factories import PLAN_DATE

    wb = generate_sample(40, PLAN_DATE, 3, tmp_path / "s.xlsx", blank_coords=False)
    monkeypatch.setenv("OSRM_URL", "http://127.0.0.1:1")
    code = cli_main(
        [
            "run",
            str(wb),
            "--preset",
            "A",
            "--plan-date",
            PLAN_DATE.isoformat(),
            "--solve-time",
            "3",
            "--provider",
            "haversine",
            "--trucks",
            "4",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "RouteOpt plan" in out and "Validator: no violations" in out


def test_cli_rejects_bad_workbook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("shipment_id,customer_name\nA_LMD,x\n")
    code = cli_main(["run", str(bad), "--solve-time", "2", "--provider", "haversine"])
    assert code == 2
    assert "Missing required columns" in capsys.readouterr().err


def test_settings_from_cli_and_sample_cmd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "gen.xlsx"
    assert (
        cli_main(
            [
                "sample",
                "--shipments",
                "20",
                "--plan-date",
                "2026-09-11",
                "--out",
                str(out),
                "--all-coords",
            ]
        )
        == 0
    )
    assert out.exists()
    s = RunSettings(plan_date=__import__("datetime").date(2026, 9, 11))
    assert s.preset == "A"


def test_cli_export_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from routeopt.db.repo import Repository
    from routeopt.sample_data import generate_sample

    from tests.factories import PLAN_DATE

    wb = generate_sample(30, PLAN_DATE, 4, tmp_path / "s.xlsx", blank_coords=False)
    db_url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("ROUTEOPT_DB_URL", db_url)
    monkeypatch.setenv("OSRM_URL", "http://127.0.0.1:1")
    out = tmp_path / "exports"
    code = cli_main(
        [
            "run",
            str(wb),
            "--plan-date",
            PLAN_DATE.isoformat(),
            "--solve-time",
            "3",
            "--provider",
            "haversine",
            "--trucks",
            "3",
            "--export",
            str(out),
            "--save",
            "--quiet",
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Exported" in printed and "Saved run" in printed
    assert list(out.glob("*.xlsx")) and list(out.glob("*.pdf"))
    repo = Repository(db_url)
    runs = repo.list_runs()
    assert len(runs) == 1 and runs[0].source_name.endswith("s.xlsx")
    rec = repo.load_run(runs[0].run_id)
    assert rec.source_bytes == wb.read_bytes() and rec.plans[0].kpis.stops_served > 0
    assert cli_main(["clear-cache"]) == 0
    assert cli_main(["osrm-check"]) == 1
