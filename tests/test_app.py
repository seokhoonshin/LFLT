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
    db_env.update_appointments({rows[0]["id"]: {"status": "Declined", "note": "no dock"}})
    ap = _run("pages/6_Appointments.py")
    assert ap.title[0].value == "Appointments"
    assert any("Declined" in m.label for m in ap.metric)
    save = next(b for b in ap.button if b.label.startswith("Save "))
    save.click().run()
    assert not ap.exception, ap.exception
    # add a stop by hand: a shipment that is not in the upload, typed in
    next(t for t in ap.text_input if t.label == "Shipment ID(s)").input("MANUAL1")
    next(t for t in ap.text_input if t.label.startswith("Customer")).input("Walk-in Co")
    next(t for t in ap.text_input if t.label.startswith("Address")).input("1 Test St, Rialto")
    next(b for b in ap.button if b.label == "Add stop as last stop").click().run()
    assert not ap.exception, ap.exception
    added = [r for r in db_env.list_appointments() if r["shipment_id"] == "MANUAL1"]
    assert added and added[0]["customer"] == "Walk-in Co"
    reopt = next(b for b in ap.button if b.label.startswith("Re-optimize "))
    assert not reopt.disabled
    reopt.click().run()
    assert not ap.exception, ap.exception
    after = db_env.list_appointments()
    assert any(r["stage"] == "reoptimized" for r in after)
    assert any(r["status"] == "Declined" and r["stage"] != "reoptimized" for r in after)
    assert any("Re-optimized" in str(sv.value) for sv in ap.success)
    # a map per load, and the finalized tab with load-ID search
    next(t for t in ap.toggle if t.label == "Map").set_value(True).run()
    assert not ap.exception, ap.exception
    first = db_env.list_appointments()[0]
    db_env.update_appointments(
        {r["id"]: {"status": "Confirmed", "load_id": "LD1"} for r in db_env.list_appointments()}
    )
    fin = _run("pages/6_Appointments.py")
    assert any(tab.label.startswith("Finalized") for tab in fin.tabs)
    next(t for t in fin.text_input if t.label == "Find a load by load ID").input("LD1").run()
    assert not fin.exception, fin.exception
    assert any(first["truck"] in str(m.value) for m in fin.markdown)
    an = _run("pages/7_Analyze.py")  # the results over time moved to Analyze
    for period in ("Week", "Month", "Year"):
        next(r for r in an.radio if r.label == "Group by").set_value(period).run()
        assert not an.exception, an.exception


def test_analyze_page_takes_both_uploads(db_env: Repository) -> None:
    import io

    import pandas as pd

    from tests.test_analyze import _ship_frame

    at = _run("pages/7_Analyze.py")
    assert at.title[0].value == "Analyze"
    ship = _ship_frame()
    load = ship.drop(columns=["SHIPMENT_ID"]).groupby(
        ["LOAD_ID", "ORIGIN_LOCATION_ID", "LOAD_STATUS", "PLAN_ID_GRP"], as_index=False
    ).agg(VOLUME=("VOLUME", "sum"), ORDER_VALUE=("ORDER_VALUE", "sum"),
          DROP_AGING_BUCKET=("DROP_AGING_BUCKET", "first"),
          RDD_AGING_BUCKET=("RDD_AGING_BUCKET", "first"),
          SNAPSHOT_DATE=("SNAPSHOT_DATE", "first"))  # fmt: skip
    import datetime as dt

    from routeopt.analyze import to_records

    db_env.save_aging("shipment", dt.date(2026, 10, 30), to_records(ship), "s.xlsx")
    db_env.save_aging("load", dt.date(2026, 10, 30), to_records(load), "l.xlsx")
    at = _run("pages/7_Analyze.py")
    assert not at.exception, at.exception
    assert len(at.dataframe) >= 4  # summary + two aging tables, both levels
    assert pd is not None and io is not None
