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


def test_group_by_stop_merges_shipments_of_one_stop() -> None:
    import datetime as dt

    from routeopt.appointments import group_by_stop

    base = {
        "run_id": "r",
        "version": 0,
        "truck": "T01",
        "load_seq": 1,
        "delivery_date": dt.date(2026, 11, 2),
        "customer": "C",
        "address": "1 A St",
        "eta": "8:00 AM",
        "window": "",
        "miles_from_prev": 5.0,
        "stop_transit_min": 0.0,
        "poc": "",
        "dc": "LGC2",
        "note": "",
    }
    rows = [
        {**base, "id": 1, "stop_seq": 1, "shipment_id": "A", "volume_cuft": 10.0,
         "bill_to_code": "B1", "status": "Confirmed"},
        {**base, "id": 2, "stop_seq": 1, "shipment_id": "B", "volume_cuft": 5.5,
         "bill_to_code": "B1", "status": "Requested"},
        {**base, "id": 3, "stop_seq": 2, "shipment_id": "C", "volume_cuft": 7.0,
         "bill_to_code": None, "status": "Declined"},
    ]  # fmt: skip
    stops = group_by_stop(rows)
    assert len(stops) == 2
    first, second = stops
    assert first["ids"] == [1, 2] and first["shipments"] == 2
    assert first["shipment_ids"] == "A, B" and first["volume_cuft"] == 15.5
    assert first["bill_to_code"] == "B1"
    assert first["status"] == "Requested"  # not every shipment is confirmed yet
    assert second["status"] == "Declined" and second["bill_to_code"] is None


def _rows_for_export() -> list[dict]:
    import datetime as dt

    base = {
        "run_id": "r",
        "version": 0,
        "truck": "T01",
        "load_seq": 1,
        "delivery_date": dt.date(2026, 11, 2),
        "address": "1 A St",
        "eta": "8:00 AM",
        "window": "",
        "miles_from_prev": 5.0,
        "stop_transit_min": 0.0,
        "poc": "",
        "dc": "LGC2",
        "note": "",
        "bill_to_code": None,
        "load_id": "19007709",
        "appt_num": None,
        "appt_time": None,
    }
    return [
        {**base, "id": 1, "stop_seq": 1, "customer": "A", "shipment_id": "a1",
         "volume_cuft": 10.0, "status": "Confirmed", "appt_time": "08:00"},
        {**base, "id": 2, "stop_seq": 1, "customer": "A", "shipment_id": "a2",
         "volume_cuft": 5.0, "status": "Confirmed", "appt_time": "08:00"},
        {**base, "id": 3, "stop_seq": 2, "customer": "B", "shipment_id": "b1",
         "volume_cuft": 7.0, "status": "Declined"},
        {**base, "id": 4, "stop_seq": 3, "customer": "C", "shipment_id": "c1",
         "volume_cuft": 3.0, "status": "Requested", "appt_num": "APT-9"},
    ]  # fmt: skip


def test_export_stops_by_load_leaves_out_declined() -> None:
    import io

    from openpyxl import load_workbook
    from routeopt.appointments import EXPORT_COLUMNS, export_stops_by_load

    ws = load_workbook(io.BytesIO(export_stops_by_load(_rows_for_export()))).active
    header = [c.value for c in ws[1]]
    assert header == EXPORT_COLUMNS
    assert header == [
        "load ID",
        "Stop",
        "date",
        "APPT time",
        "APPT #",
        "Customer",
        "Address",
        "Volume",
        "Miles from prev",
        "POC",
        "Note",
    ]
    body = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
    col = {h: i for i, h in enumerate(header)}
    wh = body[0]  # stop 1 is the warehouse with the load's total volume
    assert wh[col["Stop"]] == 1 and wh[col["Customer"]] == "LGC2 WAREHOUSE (N1F)"
    assert wh[col["Address"]] == "5550 Mango Ave, Rialto, CA 92377"
    assert wh[col["Volume"]] == "Load total volume : 18.0"  # declined B not counted
    assert wh[col["APPT #"]] is None and wh[col["load ID"]] == "19007709"
    stops = body[1:]
    assert [r[col["Customer"]] for r in stops] == ["A", "C"]  # B declined: left out
    assert [r[col["Stop"]] for r in stops] == [2, 3]
    assert stops[0][col["Volume"]] == 15.0 and stops[0][col["APPT time"]] == "8:00 AM"
    assert stops[1][col["APPT #"]] == "APT-9"
    keep = export_stops_by_load(_rows_for_export(), exclude_declined=False)
    ws2 = load_workbook(io.BytesIO(keep)).active
    assert sum(r[col["Customer"]].value == "B" for r in ws2.iter_rows(min_row=2)) == 1
    one = export_stops_by_load(_rows_for_export(), only=("r", "T01", 1))
    assert load_workbook(io.BytesIO(one)).active.max_row == 4
    none = export_stops_by_load(_rows_for_export(), only=("r", "T09", 1))
    assert load_workbook(io.BytesIO(none)).active.max_row == 1


def test_old_database_gets_new_appointment_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("create table appointments (id integer primary key, run_id varchar(64))")
    con.commit()
    con.close()
    repo = Repository(f"sqlite:///{db}")
    repo.init_schema()
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(appointments)")}
    assert {"load_id", "appt_num", "appt_time"} <= cols


def test_stop_order_and_appointment_time_are_saved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    plan, stops, _s = _plan(tmp_path)
    truck = next(t for t in plan.trucks if any(load.stops for load in t.loads))
    repo = Repository(f"sqlite:///{tmp_path / 'b.db'}")
    repo.init_schema()
    repo.select_truck_for_appointments(appointment_rows(plan, stops, truck.number))
    rows = repo.list_appointments()
    first = [r for r in rows if r["stop_seq"] == 1 and r["load_seq"] == rows[0]["load_seq"]]
    repo.update_appointments(
        {r["id"]: {"stop_seq": 9, "appt_time": "10:30", "load_id": "123"} for r in first}
    )
    again = [r for r in repo.list_appointments() if r["id"] in {x["id"] for x in first}]
    assert all(r["stop_seq"] == 9 and r["appt_time"] == "10:30" for r in again)
    assert all(r["load_id"] == "123" for r in again)


def test_map_of_one_load_draws_only_that_load(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from routeopt.export.mapping import map_html

    plan, stops, s = _plan(tmp_path)
    loads = [(t.number, ld.seq) for t in plan.trucks for ld in t.loads if ld.stops]
    number, seq = loads[0]
    one = map_html(plan, stops, s, only={(number, seq)})
    assert f"{number}·L{seq}·#1" in one
    others = [x for x in loads if x[0] != number]
    if others:
        assert f"{others[0][0]}·L{others[0][1]}·#1" not in one


def test_replan_leaves_out_declined_and_keeps_the_rest_on_their_truck(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from routeopt.pipeline import default_trucks, optimize
    from routeopt.reoptimize import replan
    from routeopt.travel import node_coords
    from routeopt.travel.haversine import HaversineProvider

    from tests.test_ca_lflt import _ca, _ship, _stops

    s = _ca(goal="stops")
    ships = [_ship(10 + 2 * i, 20 * i, address=f"{i} Rialto Rd", volume=60) for i in range(6)]
    stops = _stops(ships, s)
    raw = HaversineProvider().matrix(node_coords(stops))
    res = optimize(stops, raw, s, default_trucks()[:1])  # one truck: some stops left over
    repo = Repository(f"sqlite:///{tmp_path / 'r.db'}")
    repo.init_schema()
    repo.save_run(
        res.plan,
        res.all_stops,
        s,
        raw,
        [x for st_ in res.all_stops for x in st_.shipments],
        default_trucks()[:1],
        in_scope_ids=res.in_scope_ids,
    )
    truck = res.plan.trucks[0]
    rows = appointment_rows(res.plan, res.all_stops, truck.number)
    repo.select_truck_for_appointments(rows)
    listed = repo.list_appointments()
    gone = listed[0]["stop_id"]
    repo.update_appointments(
        {r["id"]: {"status": "Declined"} for r in listed if r["stop_id"] == gone}
    )
    rec = repo.load_run(res.plan.run_id)
    out = replan(rec, repo.list_appointments())
    served = {v.stop_id: t.number for t in out.plan.trucks for ld in t.loads for v in ld.stops}
    assert gone not in served and out.declined == [gone]
    assert all(served.get(sid) == truck.number for sid in out.pinned)  # kept on the truck
    assert out.plan.plan_version == 1 and out.plan.run_id == res.plan.run_id
    repo.save_plan_version(rec.run_id, out.plan, note="re-plan")
    new_rows = appointment_rows(out.plan, out.stops, truck.number)
    counts = repo.save_reoptimized(rec.run_id, new_rows)
    assert counts["kept"] >= 1 and counts["kept"] + counts["added"] == len(new_rows)
    after = repo.list_appointments()
    original = [r for r in after if r["stage"] != "reoptimized"]
    reopt = [r for r in after if r["stage"] == "reoptimized"]
    # the original set is the record: unchanged, the declined stop still there
    assert sorted(r["id"] for r in original) == sorted(r["id"] for r in listed)
    assert all(r["status"] == "Declined" for r in original if r["stop_id"] == gone)
    assert gone not in {r["stop_id"] for r in reopt} and {r["version"] for r in reopt} == {1}
    assert repo.selected_trucks(rec.run_id) == {truck.number}
    # one shipment, one appointment: confirming it in the re-plan confirms the original
    kept = next(r for r in reopt if r["shipment_id"] in {o["shipment_id"] for o in original})
    repo.update_appointments({kept["id"]: {"status": "Confirmed", "appt_num": "A1"}})
    twin = next(
        r
        for r in repo.list_appointments()
        if r["stage"] != "reoptimized" and r["shipment_id"] == kept["shipment_id"]
    )
    assert twin["status"] == "Confirmed" and twin["appt_num"] == "A1"
    # re-optimizing again replaces the re-plan set, it does not pile up
    repo.save_reoptimized(rec.run_id, new_rows)
    assert sum(r["stage"] == "reoptimized" for r in repo.list_appointments()) == len(new_rows)


def test_manual_rows_add_the_whole_stop_as_the_last_stop(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import pytest
    from routeopt.appointments import manual_rows

    from tests.test_ca_lflt import _ship, _stops

    plan, stops, s = _plan(tmp_path)
    truck = next(t for t in plan.trucks if any(load.stops for load in t.loads))
    rows = appointment_rows(plan, stops, truck.number)
    load1 = [r for r in rows if r["load_seq"] == 1]
    # an extra stop in the upload with two shipments at one address
    ships = [_ship(30, 200, address="9 Extra Way", volume=v) for v in (11, 22)]
    ships = [x.model_copy(update={"shipment_id": f"X{int(x.volume_cuft)}"}) for x in ships]
    extra = _stops(ships, s)
    extra = [x.model_copy(update={"stop_id": "S900"}) for x in extra]
    added = manual_rows(load1, stops + extra, " X11 ")
    assert {r["shipment_id"] for r in added} == {"X11", "X22"}  # the whole stop
    last = max(r["stop_seq"] for r in load1)
    assert {r["stop_seq"] for r in added} == {last + 1} and added[0]["stop_id"] == "S900"
    assert added[0]["status"] == "Not requested" and added[0]["note"] == "added manually"
    assert sum(r["volume_cuft"] for r in added) == 33
    with pytest.raises(ValueError, match="already on this load"):
        manual_rows(load1, stops, load1[0]["shipment_id"])
    with pytest.raises(ValueError, match="not in this run"):
        manual_rows(load1, stops, "NOPE123")
    typed = manual_rows(
        load1, stops, "NOPE123", manual={"customer": "X", "address": "1 B St", "volume_cuft": 9}
    )
    assert typed[0]["customer"] == "X" and typed[0]["stop_seq"] == last + 1
    repo = Repository(f"sqlite:///{tmp_path / 'm.db'}")
    repo.init_schema()
    assert repo.add_appointments(added + typed) == 3
    assert {r["shipment_id"] for r in repo.list_appointments()} == {"X11", "X22", "NOPE123"}
