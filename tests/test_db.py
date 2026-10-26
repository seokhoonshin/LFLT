"""M8 tests: repository round trips, immutability, soft delete, caches, alembic schema."""

from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from routeopt.db.models import Base
from routeopt.db.repo import EditLogEntry, Repository
from routeopt.domain import TruckConfig
from routeopt.ingest.validator import GeocodeResult
from routeopt.pipeline import OptimizeResult, default_trucks, optimize
from routeopt.travel import node_coords
from routeopt.travel.cache import TravelCache
from routeopt.travel.haversine import HaversineProvider

from tests.factories import make_settings, make_shipment, make_stops

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(f"sqlite:///{tmp_path}/t.db")
    r.init_schema()
    return r


@pytest.fixture(scope="module")
def solved() -> OptimizeResult:
    stops = make_stops(6, volume=40, service=30, seed=51)
    matrix = HaversineProvider().matrix(node_coords(stops))
    return optimize(stops, matrix, make_settings(solve_time_seconds=2), default_trucks()[:3])


def _save(repo: Repository, res: OptimizeResult, run_id: str | None = None) -> str:
    plan = res.plan if run_id is None else res.plan.model_copy(update={"run_id": run_id})
    shipments = [s for stop in res.all_stops for s in stop.shipments]
    repo.save_run(
        plan,
        res.all_stops,
        make_settings(solve_time_seconds=2),
        res.raw_matrix or res.full_matrix or res.matrix,
        shipments,
        default_trucks()[:3],
        source_name="sample.xlsx",
        source_bytes=b"PK\x03\x04fake",
        warnings=["a warning"],
        in_scope_ids=res.in_scope_ids,
    )
    return plan.run_id


def test_trucks_seed_and_save(repo: Repository) -> None:
    trucks = repo.list_trucks()
    assert [t.truck_number for t in trucks] == [f"T{i:02d}" for i in range(1, 11)]
    trucks[0] = TruckConfig(truck_number="T01", driver_name="Ana", active=False)
    repo.save_trucks(trucks[:5])
    again = repo.list_trucks()
    assert len(again) == 5 and again[0].driver_name == "Ana" and again[0].active is False


def test_save_and_load_run_round_trip(repo: Repository, solved: OptimizeResult) -> None:
    run_id = _save(repo, solved)
    runs = repo.list_runs()
    assert len(runs) == 1
    summary = runs[0]
    assert summary.run_id == run_id and summary.version_count == 1
    assert summary.stops_served == solved.plan.kpis.stops_served
    assert summary.run_date == solved.plan.plan_date - dt.timedelta(days=2)
    rec = repo.load_run(run_id)
    assert rec.settings.solve_time_seconds == 2
    assert [s.stop_id for s in rec.stops] == [s.stop_id for s in solved.all_stops]
    assert rec.matrix.durations_s == (solved.full_matrix or solved.matrix).durations_s
    assert rec.plans[0].model_dump() == solved.plan.model_dump()
    assert len(rec.shipments) == sum(len(s.shipments) for s in solved.all_stops)
    assert rec.trucks[0].truck_number == "T01" and rec.source_bytes == b"PK\x03\x04fake"
    assert rec.warnings == ["a warning"] and rec.in_scope_ids == solved.in_scope_ids
    assert repo.load_plan(run_id, 0).run_id == run_id


def test_v0_is_immutable(repo: Repository, solved: OptimizeResult) -> None:
    run_id = _save(repo, solved)
    with pytest.raises(ValueError):
        _save(repo, solved, run_id=run_id)
    with pytest.raises(ValueError):
        repo.save_plan_version(run_id, solved.plan)  # version 0 again
    v1 = solved.plan.model_copy(update={"plan_version": 1})
    assert (
        repo.save_plan_version(
            run_id, v1, [EditLogEntry(operation="move", before={"a": 1}, after={"b": 2})]
        )
        == 1
    )
    with pytest.raises(ValueError):
        repo.save_plan_version(run_id, v1)  # not newer than the latest
    assert repo.list_versions(run_id) == [0, 1]
    assert repo.load_plan(run_id, 1).plan_version == 1
    edits = repo.list_edits(run_id)
    assert len(edits) == 1 and edits[0].operation == "move" and edits[0].version == 1
    with pytest.raises(LookupError):
        repo.load_plan(run_id, 7)
    with pytest.raises(LookupError):
        repo.save_plan_version("nope", v1.model_copy(update={"run_id": "nope"}))


def test_exports_and_soft_delete(repo: Repository, solved: OptimizeResult) -> None:
    run_id = _save(repo, solved)
    repo.record_export(run_id, 0, "xlsx", "/tmp/x.xlsx", override_reason=None)
    repo.record_export(run_id, 0, "pdf", "/tmp/x.pdf", override_reason="customer insisted")
    exports = repo.list_exports(run_id)
    assert [e.kind for e in exports] == ["xlsx", "pdf"] and exports[1].override_reason
    repo.soft_delete_run(run_id)
    assert repo.list_runs() == []
    assert repo.list_runs(include_deleted=True)[0].deleted
    repo.restore_run(run_id)
    assert not repo.list_runs()[0].deleted
    with pytest.raises(LookupError):
        repo.soft_delete_run("missing")
    with pytest.raises(LookupError):
        repo.load_run("missing")


def test_last_settings_round_trip(repo: Repository) -> None:
    assert repo.load_last_settings() is None
    s = make_settings(preset="B", solve_time_seconds=25, pull_forward_days=[1])
    repo.save_last_settings(s)
    loaded = repo.load_last_settings()
    assert loaded is not None and loaded.preset == "B" and loaded.solve_time_seconds == 25
    repo.save_last_settings(make_settings(preset="A"))
    assert repo.load_last_settings().preset == "A"  # type: ignore[union-attr]


def test_travel_cache_store_and_clear(repo: Repository, solved: OptimizeResult) -> None:
    cache = repo.travel_cache()
    stops = make_stops(4, seed=52)
    coords = node_coords(stops)
    assert cache.get("haversine", coords) is None
    m = HaversineProvider().matrix(coords)
    cache.put(m)
    hit = cache.get("haversine", list(reversed(coords)))
    assert hit is not None and hit.durations_s[0][1] == m.durations_s[-1][-2]
    _save(repo, solved)  # references its own matrix
    removed = repo.clear_travel_cache()
    assert removed == 1  # the unreferenced one only
    assert repo.load_run(solved.plan.run_id).matrix.n == (solved.full_matrix or solved.matrix).n
    assert isinstance(TravelCache(repo.travel_cache_store()), TravelCache)


def test_geocode_cache(repo: Repository) -> None:
    gc = repo.geocode_cache()
    assert gc.get_many(["k1"]) == {}
    gc.put_many(
        [
            GeocodeResult("k1", 34.16, -117.42, "100 MAIN ST", "Exact"),
            GeocodeResult("k2", None, None, match_type="No_Match"),
        ]
    )
    got = gc.get_many(["k1", "k2"])
    assert set(got) == {"k1"} and got["k1"].from_cache and got["k1"].latitude == 34.16
    gc.put_many([GeocodeResult("k1", 34.17, -117.43, "100 MAIN ST", "Exact")])
    assert gc.get_many(["k1"])["k1"].latitude == 34.17
    assert repo.clear_geocode_cache() == 1


def test_load_run_without_cached_matrix_raises(repo: Repository, solved: OptimizeResult) -> None:
    run_id = _save(repo, solved)
    from routeopt.db.models import TravelCacheRow
    from sqlalchemy import delete

    with repo._sf() as session, session.begin():
        session.execute(delete(TravelCacheRow))
    with pytest.raises(LookupError):
        repo.load_run(run_id)


def test_shipment_snapshot_uses_make_shipment(repo: Repository) -> None:
    # sanity: a shipment JSON round trip through the run_shipments table format
    s = make_shipment()
    assert s.model_validate_json(s.model_dump_json()) == s


def test_alembic_upgrade_matches_create_all(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    env = {"ROUTEOPT_DB_URL": f"sqlite:///{db}", "PATH": str(Path(sys.executable).parent)}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    tables = {
        r[0]
        for r in sqlite3.connect(db).execute("select name from sqlite_master where type='table'")
    }
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_osrm_traffic_factor_applied_once_and_cache_stays_raw(repo: Repository) -> None:
    from routeopt.config import RunSettings
    from routeopt.travel import planning_matrix
    from routeopt.travel.cache import TravelCache

    from tests.factories import PLAN_DATE

    settings = RunSettings(plan_date=PLAN_DATE, solve_time_seconds=2)  # CA LFLT: x 1.3
    stops = make_stops(4, volume=40, service=30, seed=52, settings=settings)
    raw = HaversineProvider().matrix(node_coords(stops)).model_copy(update={"source": "osrm"})
    res = optimize(stops, raw, settings, default_trucks()[:2])
    assert res.raw_matrix is raw
    a, b = 2, 3  # two stops
    assert res.full_matrix is not None
    assert res.full_matrix.durations_s[a][b] == round(raw.durations_s[a][b] * 1.3)
    shipments = [s for stop in res.all_stops for s in stop.shipments]
    repo.save_run(
        res.plan, res.all_stops, settings, res.raw_matrix, shipments, default_trucks()[:2]
    )
    from routeopt.db.repo import DbTravelCacheStore

    cached = TravelCache(DbTravelCacheStore(repo._sf)).get("osrm", node_coords(res.all_stops))
    assert cached is not None and cached.durations_s == raw.durations_s  # raw in the cache
    rec = repo.load_run(res.plan.run_id)
    assert rec.matrix.durations_s == planning_matrix(raw, settings).durations_s  # x 1.3 once
