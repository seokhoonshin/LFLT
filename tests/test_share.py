"""Team sharing of Appointments / Analyze through a git branch (real git, local remote)."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest
from routeopt.db.repo import Repository
from routeopt.share import fetch, sync

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _clone(remote: Path, where: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(where)], check=True)
    return where


def _repo(path: Path) -> Repository:
    r = Repository(f"sqlite:///{path}")
    r.init_schema()
    return r


def _row(shipment: str, status: str = "Not requested") -> dict:
    return {
        "run_id": "R1",
        "version": 0,
        "truck": "T01",
        "load_seq": 1,
        "stop_seq": 1,
        "stop_id": "S1",
        "shipment_id": shipment,
        "customer": "C",
        "address": "1 A St",
        "bill_to_code": None,
        "delivery_date": dt.date(2026, 11, 4),
        "eta": "8:30 AM",
        "window": "",
        "volume_cuft": 10.0,
        "miles_from_prev": 5.0,
        "stop_transit_min": 0.0,
        "poc": "",
        "dc": "LGC2",
        "status": status,
    }


def test_two_editors_share_and_a_late_save_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    a_root, b_root = _clone(remote, tmp_path / "a"), _clone(remote, tmp_path / "b")
    a, b = _repo(tmp_path / "a.db"), _repo(tmp_path / "b.db")

    # A picks a truck: the first sync pushes it; B loads it
    a.select_truck_for_appointments([_row("X1"), _row("X2")])
    a.save_aging("perf", dt.date(2026, 11, 3), [{"LOAD_ID": "1", "STOPS": 3}], "p.xlsx")
    assert sync(a, a_root, "editor", "openmile-shared") == "pushed"
    assert sync(b, b_root, "editor", "openmile-shared") == "loaded"
    assert {r["shipment_id"] for r in b.list_appointments()} == {"X1", "X2"}
    assert b.list_aging("perf")[0][1] == 1
    assert sync(b, b_root, "editor", "openmile-shared") == "same"

    # B confirms X1 and pushes; A loads it
    x1 = next(r for r in b.list_appointments() if r["shipment_id"] == "X1")
    b.update_appointments({x1["id"]: {"status": "Confirmed"}})
    assert sync(b, b_root, "editor", "openmile-shared") == "pushed"
    assert sync(a, a_root, "editor", "openmile-shared") == "loaded"
    assert (
        next(r for r in a.list_appointments() if r["shipment_id"] == "X1")["status"] == "Confirmed"
    )

    # both change at once: B pushes first; A's change is refused, A gets B's data
    xa = next(r for r in a.list_appointments() if r["shipment_id"] == "X2")
    a.update_appointments({xa["id"]: {"status": "Declined"}})
    xb = next(r for r in b.list_appointments() if r["shipment_id"] == "X2")
    b.update_appointments({xb["id"]: {"status": "Pending"}})
    assert sync(b, b_root, "editor", "openmile-shared") == "pushed"
    assert sync(a, a_root, "editor", "openmile-shared", pull=False) == "conflict"
    assert next(r for r in a.list_appointments() if r["shipment_id"] == "X2")["status"] == "Pending"

    # a viewer only loads, and never pushes
    v_root, v = _clone(remote, tmp_path / "v"), _repo(tmp_path / "v.db")
    assert sync(v, v_root, "viewer", "openmile-shared") == "loaded"
    v.update_appointments({v.list_appointments()[0]["id"]: {"status": "Declined"}})
    before = fetch(v_root, "openmile-shared")
    assert sync(v, v_root, "viewer", "openmile-shared") == "same"
    assert fetch(v_root, "openmile-shared") == before


def test_a_zip_folder_cannot_share(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from routeopt.share import ShareError

    with pytest.raises(ShareError, match="not a git clone"):
        sync(_repo(tmp_path / "z.db"), tmp_path, "editor", "openmile-shared")
