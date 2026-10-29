"""Appointments: picking a truck on Results and tracking each shipment's status."""

from __future__ import annotations

from routeopt.appointments import STATUSES, appointment_rows
from routeopt.db.repo import Repository

from tests.test_load_summary import _plan


def test_picked_truck_is_listed_and_statuses_are_saved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    plan, stops, _s = _plan(tmp_path)
    stops = [
        s.model_copy(
            update={
                "shipments": [sh.model_copy(update={"bill_to_code": "BT1"}) for sh in s.shipments]
            }
        )
        for s in stops
    ]
    truck = next(t for t in plan.trucks if any(load.stops for load in t.loads))
    rows = appointment_rows(plan, stops, truck.number)
    n_ship = sum(
        len(next(s for s in stops if s.stop_id == v.stop_id).shipments)
        for load in truck.loads
        for v in load.stops
    )
    assert len(rows) == n_ship
    first = rows[0]
    assert first["truck"] == truck.number and first["load_seq"] == 1 and first["stop_seq"] == 1
    assert first["bill_to_code"] == "BT1" and first["delivery_date"] == plan.plan_date
    assert first["stop_transit_min"] == 0 and first["eta"]

    repo = Repository(f"sqlite:///{tmp_path / 'a.db'}")
    repo.init_schema()
    assert repo.select_truck_for_appointments(rows) == n_ship
    assert repo.select_truck_for_appointments(rows) == 0  # picking again adds nothing
    assert repo.selected_trucks(plan.run_id) == {truck.number}
    listed = repo.list_appointments()
    assert [r["status"] for r in listed] == [STATUSES[0]] * n_ship
    changed = repo.update_appointments({listed[0]["id"]: {"status": "Confirmed", "note": "ok"}})
    assert changed == 2
    again = repo.list_appointments()[0]
    assert again["status"] == "Confirmed" and again["note"] == "ok"
    repo.unselect_truck(plan.run_id, truck.number)
    assert repo.list_appointments() == [] and repo.selected_trucks(plan.run_id) == set()


def test_stop_outcomes_and_summary() -> None:
    import datetime as dt

    from routeopt.appointments import outcome_summary, stop_outcomes

    day = dt.date(2026, 10, 30)

    def row(stop: int, status: str, vol: float) -> dict:
        return {
            "run_id": "r",
            "truck": "T01",
            "load_seq": 1,
            "stop_seq": stop,
            "delivery_date": day,
            "customer": "c",
            "volume_cuft": vol,
            "status": status,
        }

    rows = [
        row(1, "Confirmed", 100),
        row(1, "Confirmed", 50),
        row(2, "Confirmed", 10),
        row(2, "Declined", 30),  # one declined shipment drops the stop
        row(3, "Pending", 60),
        row(4, "Confirmed", 50),
    ]
    stops = {s["stop_seq"]: s for s in stop_outcomes(rows)}
    assert [stops[i]["outcome"] for i in (1, 2, 3, 4)] == [
        "Confirmed",
        "Declined",
        "Open",
        "Confirmed",
    ]
    summary = outcome_summary(list(stops.values()))
    assert summary["Confirmed"] == {
        "stops": 2,
        "stops_pct": 50.0,
        "volume": 200.0,
        "volume_pct": 66.7,
    }
    assert summary["Declined"]["stops"] == 1 and summary["Declined"]["stops_pct"] == 25.0
    assert summary["Open"]["volume"] == 60.0
