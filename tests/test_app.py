"""M5 smoke tests: every Streamlit page renders without exceptions (AppTest, headless)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from routeopt.db.repo import Repository
from routeopt.domain import hub_today
from routeopt.pipeline import default_trucks, optimize
from routeopt.sample_data import generate_sample
from routeopt.travel import node_coords
from routeopt.travel.haversine import HaversineProvider
from streamlit.testing.v1 import AppTest

from tests.factories import make_settings, make_stops

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.setenv("ROUTEOPT_DB_URL", f"sqlite:///{tmp_path}/app.db")
    monkeypatch.setenv("ROUTEOPT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OSRM_URL", "http://127.0.0.1:1")
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    repo = Repository(f"sqlite:///{tmp_path}/app.db")
    repo.init_schema()
    return repo


def _saved_run(repo: Repository) -> str:
    stops = make_stops(6, volume=40, service=30, seed=71)
    matrix = HaversineProvider().matrix(node_coords(stops))
    settings = make_settings(solve_time_seconds=2)
    res = optimize(stops, matrix, settings, default_trucks()[:3])
    repo.save_run(
        res.plan,
        res.all_stops,
        settings,
        res.full_matrix or res.matrix,
        [s for st_ in res.all_stops for s in st_.shipments],
        default_trucks()[:3],
        source_name="t.xlsx",
        source_bytes=b"x",
        in_scope_ids=res.in_scope_ids,
    )
    return res.plan.run_id


def _run(page: str, run_id: str | None = None, **state: object) -> AppTest:
    at = AppTest.from_file(str(APP / page), default_timeout=120)
    if run_id:
        at.session_state["current_run_id"] = run_id
        at.session_state["current_version"] = 0
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    return at


def test_home_and_trucks_pages(db_env: Repository) -> None:
    at = _run("Home.py")
    assert any("RouteOpt" in t.value for t in at.title)
    at2 = _run("pages/1_Trucks_Drivers.py")
    assert at2.title[0].value == "Trucks & Drivers"


def test_new_run_page_validates_upload(db_env: Repository, tmp_path: Path) -> None:
    wb = generate_sample(
        30, hub_today() + dt.timedelta(days=2), 5, tmp_path / "s.xlsx", blank_coords=False
    )
    at = _run(
        "pages/2_New_Run.py",
        upload_bytes=wb.read_bytes(),
        upload_name="s.xlsx",
        upload_digest="abc",
        mapping={},
        exclude_rows=set(),
    )
    texts = " ".join(str(getattr(e, "value", "")) for e in at.success)
    assert "shipments ->" in texts
    assert any(b.label == "Optimize" for b in at.button)


def test_results_edit_history_pages(db_env: Repository) -> None:
    run_id = _saved_run(db_env)
    at = _run("pages/3_Results.py", run_id)
    assert any(run_id in t.value for t in at.title)
    at2 = _run("pages/4_Edit.py", run_id)
    assert any(run_id in t.value for t in at2.title)
    at3 = _run("pages/5_History.py", run_id)
    assert at3.title[0].value == "History"


def test_select_truck_then_appointments_page(db_env: Repository) -> None:
    run_id = _saved_run(db_env)
    at = _run("pages/6_Appointments.py")
    assert any("No trucks picked" in str(i.value) for i in at.info)
    res = _run("pages/3_Results.py", run_id)
    pick = next(b for b in res.button if b.label == "Select this truck")
    pick.click().run()
    assert not res.exception, res.exception
    assert any(b.label == "✓ Selected" for b in res.button)
    rows = db_env.list_appointments()
    assert rows and {r["run_id"] for r in rows} == {run_id}
    ap = _run("pages/6_Appointments.py")
    assert ap.title[0].value == "Appointments"
    save = next(b for b in ap.button if b.label.startswith("Save "))
    save.click().run()
    assert not ap.exception, ap.exception
