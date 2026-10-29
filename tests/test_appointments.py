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
