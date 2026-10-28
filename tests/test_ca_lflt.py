"""CA LFLT profile: two DCs, pallet loads, two turns, distance limits, upload columns."""

from __future__ import annotations

import datetime as dt

from routeopt.config import RunSettings
from routeopt.domain import DayTag, OrderType, Shipment, Stop, clock
from routeopt.ingest.consolidate import LOOK_UP_CONTACT, consolidate
from routeopt.ingest.excel_loader import RawRow
from routeopt.ingest.validator import validate_rows
from routeopt.pipeline import OptimizeResult, default_trucks, optimize
from routeopt.solver.plan_validator import validate_plan
from routeopt.travel import node_coords
from routeopt.travel.haversine import HaversineProvider

from tests.factories import PLAN_DATE, make_shipment, point_at, renumber

SOLVE = 5
FAST = HaversineProvider(1.3, 55.0)  # highway-ish speed so long CA runs fit the day


def _ca(**kw: object) -> RunSettings:
    return RunSettings(plan_date=PLAN_DATE, solve_time_seconds=SOLVE, **kw)  # type: ignore[arg-type]


def _ship(
    miles: float, bearing: float, *, site: str = "LGC2", pallet: bool = False, **kw: object
) -> Shipment:
    lat, lon = point_at(miles, bearing)
    sh = make_shipment(lat=lat, lon=lon, order_type=OrderType.LTL, **kw)  # type: ignore[arg-type]
    return sh.model_copy(update={"site": site, "pallet_required": pallet})


def _stops(shipments: list[Shipment], settings: RunSettings) -> list[Stop]:
    return renumber(consolidate(shipments, settings).stops)


def _run(stops: list[Stop], settings: RunSettings, trucks: int = 2) -> OptimizeResult:
    matrix = FAST.matrix(node_coords(stops))
    return optimize(stops, matrix, settings, default_trucks()[:trucks])


def _loads(res: OptimizeResult):  # type: ignore[no-untyped-def]
    return [(t, load) for t in res.plan.trucks for load in t.loads if load.stops]


def test_unloading_minutes_come_from_volume() -> None:
    s = _ca()
    stops = _stops([_ship(10, 0, address="1 A St", volume=46.3, service=99)], s)
    assert stops[0].service_total_minutes == 5  # 46.3 x 0.1 rounded; no stop buffer in CA


def test_two_dcs_never_share_a_load_and_the_driver_start_floats() -> None:
    s = _ca()
    ships = [_ship(15, 30 * i, address=f"{i} Rialto Rd", volume=60) for i in range(5)]
    ships += [
        _ship(15, 30 * i + 15, site="LGC8", address=f"{i} Fontana Rd", volume=60) for i in range(5)
    ]
    stops = _stops(ships, s)
    by_id = {x.stop_id: x for x in stops}
    res = _run(stops, s)
    assert not res.plan.hard_violations
    assert len(res.plan.served_stop_ids) == 10
    for _truck, load in _loads(res):
        assert {by_id[v.stop_id].site for v in load.stops} == {load.site}
    for truck in res.plan.used_trucks:
        first = truck.loads[0]
        assert truck.start_s is not None
        assert first.depart_s == truck.start_s + 90 * 60  # 90 min first loading
        # start = 08:00 appointment - loading - drive: the first stop is reached at 08:00
        assert clock(first.stops[0].arrive_s) == "8:00 AM"
        assert all(v.arrive_s >= 8 * 3600 for ld in truck.loads for v in ld.stops)
        assert abs(truck.on_duty_minutes - (truck.loads[-1].return_s - truck.start_s) / 60) < 0.1


def test_pallet_loads_stay_below_400() -> None:
    s = _ca()
    ships = [_ship(8, 90 * i, address=f"{i} Pallet Way", volume=300, pallet=True) for i in range(3)]
    ships += [_ship(9, 90 * i + 45, address=f"{i} Box Ave", volume=150) for i in range(3)]
    stops = _stops(ships, s)
    res = _run(stops, s, trucks=2)
    assert not res.plan.hard_violations
    for _truck, load in _loads(res):
        if load.pallet:
            assert load.load_out_volume < 400
        else:
            assert load.load_out_volume < 800


def test_two_turn_gap_and_total_miles() -> None:
    # 1,200 cu ft near the DC for one truck: it must run a second turn
    s = _ca()
    ships = [_ship(10 + i, 40 * i, address=f"{i} Near St", volume=150) for i in range(8)]
    stops = _stops(ships, s)
    res = _run(stops, s, trucks=1)
    assert not res.plan.hard_violations
    truck = res.plan.trucks[0]
    loads = [ld for ld in truck.loads if ld.stops]
    assert len(loads) == 2
    first, second = loads
    assert first.last_delivery_s is not None
    assert second.depart_s >= first.last_delivery_s + 180 * 60
    assert second.depart_s - (second.reload_start_s or 0) >= s.reload_seconds
    assert sum(ld.distance_miles for ld in loads) < 300
    assert truck.on_duty_minutes <= 600


def test_one_way_limit_and_carrier_check() -> None:
    s = _ca()
    near = _ship(20, 0, address="1 Near Rd", volume=50)
    mid = _ship(130, 90, address="2 Mid Rd", volume=50)  # ~169 road mi
    far = _ship(160, 180, address="3 Far Rd", volume=50)  # ~208 road mi
    stops = _stops([near, mid, far], s)
    res = _run(stops, s)
    served = set(res.plan.served_stop_ids)
    by_addr = {x.address: x.stop_id for x in stops}
    assert by_addr["3 Far Rd"] not in served
    assert by_addr["2 Mid Rd"] in served
    assert not res.plan.hard_violations
    assert any(v.code == "carrier_check" for v in res.plan.violations)


def test_validator_flags_ca_rule_breaks() -> None:
    s = _ca()
    ships = [
        _ship(10, 0, address="1 A St", volume=100),
        _ship(10, 180, address="2 B St", volume=100),
    ]
    ships.append(_ship(12, 90, site="LGC8", address="3 C St", volume=100))
    stops = _stops(ships, s)
    res = _run(stops, s)
    plan = res.plan.model_copy(deep=True)
    # put the Fontana stop on a Rialto load: mixed sites
    truck = next(t for t in plan.trucks if t.used)
    from routeopt.solver.extract import rebuild_plan_trucks

    load_ids = [[v.stop_id for v in ld.stops] for ld in truck.loads if ld.stops]
    fontana = next(x.stop_id for x in stops if x.site == "LGC8")
    rialto_load = next(ld for ld in load_ids if fontana not in ld)
    for ld in load_ids:
        if fontana in ld:
            ld.remove(fontana)
    rialto_load.append(fontana)
    from routeopt.solver.timeline import RouteSkeleton, build_truck, node_index_map

    rebuilt = build_truck(
        RouteSkeleton(truck.number, loads=[ld for ld in load_ids if ld]),
        {x.stop_id: x for x in stops},
        res.matrix,
        node_index_map(stops),
        s,
    )
    plan.trucks = [rebuilt if t.number == truck.number else t for t in plan.trucks]
    plan = rebuild_plan_trucks(plan, stops, s, res.matrix)
    codes = {v.code for v in validate_plan(plan, stops, s, res.matrix)}
    assert "site" in codes


def _raw(n: int, **kw: object) -> RawRow:
    base: dict[str, object] = {
        "shipment_id": f"3K0{n:07d}",
        "delivery_date": PLAN_DATE,
        "order_type": "LTL",
        "customer_name": "Ferguson",
        "address": f"{n} Main St",
        "city": "Anaheim",
        "state": "CA",
        "zip": "92805",
        "latitude": 33.83,
        "longitude": -117.91,
        "volume_cuft": 46.3,
    }
    base.update(kw)
    return RawRow(n, base)


def test_ca_upload_columns() -> None:
    s = _ca()
    rows = [
        _raw(2, origin_dc="LGC8", pallet_required="Y", priority=1, memo="Call Ana 714-555-0101"),
        _raw(3, origin_dc="lgc7", priority=2),
        _raw(4, origin_dc="LGC9"),
        _raw(5, pallet_required="maybe"),
        _raw(6, volume_cuft=400, pallet_required="ES"),
        _raw(7, origin_dc="LGC2", pallet_required="HA", volume_cuft=600, address="7 Ha St"),
        _raw(8, origin_dc="LGC2", pallet_required="ac", volume_cuft=300, address="8 Ac St"),
    ]
    res = validate_rows(rows, PLAN_DATE, s)
    by_row = {e.row: e.message for e in res.errors}
    assert set(by_row) == {4, 5, 6}
    assert "Unknown DC" in by_row[4] and "Y / N" in by_row[5] and "less than 400" in by_row[6]
    a, b, ha, ac = res.shipments
    assert not ha.pallet_required and ac.pallet_required  # HA: no pallet; others: pallet
    assert (a.site, a.pallet_required, a.priority) == ("LGC8", True, 1)
    assert (b.site, b.origin_dc, b.priority) == ("LGC7", "LGC7", 2)
    assert a.shipment_type.value == "LMD" and a.weight_lb == 0 and a.pieces == 1
    stops = consolidate([a, b], s).stops
    by_site = {x.site: x for x in stops}
    assert by_site["LGC8"].tier == 1 and by_site["LGC8"].pallet
    assert by_site["LGC8"].contact == "Ana 714-555-0101"
    assert by_site["LGC7"].tier == 2 and by_site["LGC7"].contact == LOOK_UP_CONTACT
    assert all(x.day_tag == DayTag.D for x in stops)


def test_ca_profile_rejects_pull_forward_free_settings_roundtrip() -> None:
    s = _ca(shift_start=dt.time(6, 30))
    assert s.shift_start == dt.time(6, 30) and s.first_load_minutes == 90


def test_lgc2_and_lgc7_are_separate_dcs_at_one_address() -> None:
    s = _ca()
    ships = [_ship(12, 40 * i, address=f"{i} Two Rd", volume=60) for i in range(4)]
    ships += [
        _ship(12, 40 * i + 20, site="LGC7", address=f"{i} Seven Rd", volume=60) for i in range(4)
    ]
    ships.append(_ship(12, 0, site="LGC7", address="0 Two Rd", volume=60))  # same address, other DC
    stops = _stops(ships, s)
    by_id = {x.stop_id: x for x in stops}
    assert len(stops) == 9  # one address served from LGC2 and from LGC7 is two stops
    res = _run(stops, s)
    assert not res.plan.hard_violations
    for _truck, load in _loads(res):
        assert {by_id[v.stop_id].site for v in load.stops} == {load.site}


def test_saved_runs_with_old_site_codes_still_load() -> None:
    from routeopt.domain import Load, Shipment

    old = make_shipment().model_dump()
    old["site"] = "RIALTO"
    assert Shipment.model_validate(old).site == "LGC2"
    assert (
        Load(
            seq=1, depart_s=0, return_s=0, load_out_volume=0, max_volume_on_road=0, site="FONTANA"
        ).site
        == "LGC8"
    )


def test_driver_start_follows_the_first_stop_and_prefers_a_near_one() -> None:
    s = _ca()
    stops = _stops([_ship(5, 0, address="1 Near Rd"), _ship(60, 90, address="2 Far Rd")], s)
    res = _run(stops, s, trucks=1)
    assert not res.plan.violations
    truck = res.plan.used_trucks[0]
    first = truck.loads[0].stops[0]
    assert next(x for x in stops if x.stop_id == first.stop_id).address == "1 Near Rd"
    assert clock(first.arrive_s) == "8:00 AM"
    # start = 08:00 - 90 min loading - drive to the near stop: after 06:00
    assert 6 * 3600 <= truck.start_s < 6.5 * 3600
    assert abs(truck.on_duty_minutes - (truck.loads[-1].return_s - truck.start_s) / 60) < 0.1


def test_driver_never_starts_before_the_dc_opens() -> None:
    s = _ca()
    stops = _stops([_ship(60, 90, address="2 Far Rd")], s)
    res = _run(stops, s, trucks=1)
    assert not res.plan.violations
    truck = res.plan.used_trucks[0]
    load = truck.loads[0]
    assert truck.start_s == 6 * 3600  # DC opens at 06:00
    assert load.depart_s == 7 * 3600 + 30 * 60  # + 90 min loading
    assert load.stops[0].arrive_s == load.depart_s + round(load.stops[0].travel_min_from_prev * 60)
    assert load.stops[0].arrive_s > 8 * 3600
    assert load.return_s - truck.start_s <= 10 * 3600


def test_volume_value_keeps_the_big_stop_when_trucks_are_short() -> None:
    # one truck, one load: the 700 cu ft stop and three 60 cu ft stops cannot all fit
    ships = [_ship(20, 0, address="1 Big Rd", volume=700)]
    ships += [_ship(20, 90 + 20 * i, address=f"{i} Small St", volume=60) for i in range(3)]

    def served_volume(**kw: object) -> tuple[int, float]:
        s = _ca(max_loads_per_truck=1, **kw)
        stops = _stops(ships, s)
        res = _run(stops, s, trucks=1)
        by_id = {x.stop_id: x for x in stops}
        served = res.plan.served_stop_ids
        return len(served), sum(by_id[i].delivery_volume for i in served)

    assert served_volume(volume_value_cuft=None) == (3, 180)  # count only: the small ones
    assert served_volume() == (2, 760)  # default 100 cu ft = 1 stop: big + one small


def test_stop_penalty_scales_priority_2_by_volume() -> None:
    s = _ca()
    assert s.stop_penalty(2, 300) == 4 * s.penalty_for_tier(2)
    assert s.stop_penalty(1, 0) > s.stop_penalty(2, 800)
    assert _ca(volume_value_cuft=None).stop_penalty(2, 300) == s.penalty_for_tier(2)


def test_stop_transit_is_shown_apart_from_driving() -> None:
    s = _ca()
    ships = [_ship(10, 0, address="1 A St", volume=50), _ship(12, 10, address="2 B St", volume=50)]
    stops = _stops(ships, s)
    res = _run(stops, s, trucks=1)
    visits = [v for _t, load in _loads(res) for v in load.stops]
    assert len(visits) == 2
    assert visits[0].transit_min_from_prev == 0
    assert visits[1].transit_min_from_prev == 20
    assert visits[1].depart_s - visits[1].arrive_s == 5 * 60  # service = 50 x 0.1
