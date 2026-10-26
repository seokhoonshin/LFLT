"""M7 tests: manual editing operations, re-validation, versions and the export gate."""

from __future__ import annotations

import pytest
from routeopt.edit.operations import (
    REMOVED_BY_PLANNER,
    EditContext,
    EditError,
    assign_stop,
    locate,
    move_down,
    move_stop,
    move_up,
    reorder_stop,
    unassign_stop,
)
from routeopt.edit.revalidate import (
    can_export,
    diff_plans,
    new_version,
    revalidate,
    set_export_override,
    violations_by_stop,
)
from routeopt.pipeline import default_trucks, optimize
from routeopt.travel import node_coords
from routeopt.travel.haversine import HaversineProvider

from tests.factories import make_settings, make_stops, renumber


@pytest.fixture(scope="module")
def ctx() -> EditContext:
    # three heavy stops (350 cu ft) + five light ones: any load with two heavy stops overflows
    heavy = make_stops(3, volume=350, service=30, max_miles=8, seed=61, prefix="H")
    light = make_stops(5, volume=30, service=30, max_miles=8, seed=62, prefix="L")
    stops = renumber(heavy + light)
    matrix = HaversineProvider().matrix(node_coords(stops))
    settings = make_settings(solve_time_seconds=3, max_loads_per_truck=2)
    res = optimize(stops, matrix, settings, default_trucks()[:4])
    assert not res.plan.unassigned and res.plan.violations == []
    return EditContext(plan=res.plan, stops=stops, settings=settings, matrix=matrix)


def _heavy_locations(ctx: EditContext) -> list[tuple[str, str, int]]:
    out = []
    for sid in ("S001", "S002", "S003"):
        loc = locate(ctx.plan, sid)
        assert loc is not None
        out.append((sid, loc.truck, loc.load_seq))
    return out


def test_move_overflows_volume_and_blocks_export(ctx: EditContext) -> None:
    (_sid_a, truck_a, load_a), (sid_b, truck_b, load_b), _ = _heavy_locations(ctx)
    # ensure two heavy stops end up on the same load
    if (truck_a, load_a) == (truck_b, load_b):
        pytest.skip("solver already put two heavy stops together")
    plan, entry = move_stop(ctx, sid_b, truck_a, load_a)
    assert entry.operation == "move" and entry.before["stop_id"] == sid_b
    assert entry.after["truck"] == truck_a
    checked = revalidate(ctx, plan)
    codes = {v.code for v in checked.violations if v.severity == "error"}
    assert "volume" in codes
    gate = can_export(checked)
    assert not gate.allowed and "Export anyway" in gate.message
    overridden = set_export_override(checked, "customer insisted")
    assert (
        can_export(overridden).allowed and overridden.export_override_reason == "customer insisted"
    )
    with pytest.raises(EditError):
        set_export_override(checked, "   ")
    keyed = violations_by_stop(checked)
    assert any(k.startswith(truck_a) or k == sid_b or k.startswith("S") for k in keyed)
    # the original (v0) plan is untouched
    assert ctx.plan.violations == [] and can_export(ctx.plan).allowed
    assert locate(ctx.plan, sid_b) is not None and locate(ctx.plan, sid_b).truck == truck_b  # type: ignore[union-attr]
    lines = diff_plans(ctx.plan, checked)
    assert any(sid_b in line and "moved" in line for line in lines)


def test_reorder_recomputes_sequence_and_times(ctx: EditContext) -> None:
    truck = next(t for t in ctx.plan.used_trucks if len(t.loads[0].stops) >= 2)
    load = truck.loads[0]
    first = load.stops[0].stop_id
    plan, entry = move_down(ctx, truck.number, load.seq, first)
    assert entry.operation == "reorder"
    checked = revalidate(ctx, plan)
    new_load = checked.truck(truck.number).loads[0]
    assert new_load.stops[1].stop_id == first
    assert [v.seq for v in new_load.stops] == list(range(1, len(new_load.stops) + 1))
    assert all(
        a.arrive_s < b.arrive_s for a, b in zip(new_load.stops, new_load.stops[1:], strict=False)
    )
    back, _ = move_up(
        EditContext(checked, ctx.stops, ctx.settings, ctx.matrix), truck.number, load.seq, first
    )
    assert back.truck(truck.number).loads[0].stops[0].stop_id == first
    plan2, _ = reorder_stop(ctx, truck.number, load.seq, first, 99)  # clamps to the end
    assert plan2.truck(truck.number).loads[0].stops[-1].stop_id == first
    with pytest.raises(EditError):
        reorder_stop(ctx, "T99", 1, first, 0)


def test_unassign_and_assign_back(ctx: EditContext) -> None:
    truck = ctx.plan.used_trucks[0]
    sid = truck.loads[0].stops[-1].stop_id
    plan, entry = unassign_stop(ctx, sid)
    assert entry.operation == "unassign"
    checked = revalidate(ctx, plan)
    unassigned = [u for u in checked.unassigned if u.stop_id == sid]
    assert unassigned and unassigned[0].reason == REMOVED_BY_PLANNER
    assert checked.kpis.stops_served == ctx.plan.kpis.stops_served - 1
    ctx2 = EditContext(checked, ctx.stops, ctx.settings, ctx.matrix)
    plan2, entry2 = assign_stop(ctx2, sid, truck.number, 1)
    assert entry2.operation == "assign"
    checked2 = revalidate(ctx2, plan2)
    assert set(checked2.served_stop_ids) == set(ctx.plan.served_stop_ids)
    assert checked2.kpis.stops_served == ctx.plan.kpis.stops_served
    assert all(u.stop_id != sid for u in checked2.unassigned)
    with pytest.raises(EditError):
        assign_stop(ctx, sid, truck.number, 1)  # already on a route
    with pytest.raises(EditError):
        unassign_stop(ctx, "S999")


def test_move_to_new_load_creates_reload(ctx: EditContext) -> None:
    used = ctx.plan.used_trucks
    single = min(used, key=lambda t: len([load for load in t.loads if load.stops]))
    n_loads = len([load for load in single.loads if load.stops])
    if n_loads >= ctx.settings.max_loads_per_truck:
        pytest.skip("every truck already runs the maximum number of loads")
    donor = next(t for t in used if t.number != single.number)
    sid = donor.loads[0].stops[0].stop_id
    plan, _ = move_stop(ctx, sid, single.number, n_loads + 1)
    checked = revalidate(ctx, plan)
    t = checked.truck(single.number)
    loads = [load for load in t.loads if load.stops]
    assert len(loads) == n_loads + 1 and loads[-1].stops[0].stop_id == sid
    assert loads[-1].reload_start_s == loads[-2].return_s
    assert loads[-1].depart_s - loads[-2].return_s == ctx.settings.reload_seconds
    assert t.lunch.kind == "reload"
    # one more load than allowed is refused (max_loads_per_truck = 2)
    ctx2 = EditContext(checked, ctx.stops, ctx.settings, ctx.matrix)
    other = next(x for x in checked.used_trucks if x.number != single.number)
    with pytest.raises(EditError):
        move_stop(ctx2, other.loads[0].stops[0].stop_id, single.number, 3)
    with pytest.raises(EditError):
        move_stop(ctx2, other.loads[0].stops[0].stop_id, single.number, 7)
    with pytest.raises(EditError):
        move_stop(ctx, "S999", single.number, 1)
    with pytest.raises(EditError):
        move_stop(ctx, sid, "T77", 1)


def test_new_version_and_diff(ctx: EditContext) -> None:
    v1 = new_version(set_export_override(ctx.plan, "x"), 1)
    assert v1.plan_version == 1 and v1.export_override_reason is None
    assert diff_plans(ctx.plan, v1) == []
    truck = ctx.plan.used_trucks[0]
    sid = truck.loads[0].stops[0].stop_id
    plan, _ = unassign_stop(ctx, sid)
    lines = diff_plans(ctx.plan, plan)
    assert lines and "removed" in lines[0] and sid in lines[0]
    lines2 = diff_plans(plan, ctx.plan)
    assert "assigned" in lines2[0]


def test_out_of_scope_stops_are_not_turned_into_unassigned_by_edits() -> None:
    from routeopt.domain import DayTag

    d = make_stops(4, volume=40, service=30, seed=81, prefix="D")
    d1 = make_stops(2, volume=40, service=30, seed=82, prefix="E", day_offset=1)
    stops = renumber(d + d1)
    matrix = HaversineProvider().matrix(node_coords(stops))
    settings = make_settings(solve_time_seconds=2, pull_forward_days=[])
    res = optimize(stops, matrix, settings, default_trucks()[:2])
    assert len(res.plan.out_of_scope) == 2 and not res.plan.unassigned
    ctx = EditContext(res.plan, stops, settings, matrix)  # full stop list, as the UI does
    sid = res.plan.used_trucks[0].loads[0].stops[0].stop_id
    plan, _ = unassign_stop(ctx, sid)
    checked = revalidate(ctx, plan)
    assert [u.stop_id for u in checked.unassigned] == [sid]
    assert len(checked.out_of_scope) == 2
    # assigning an out-of-scope stop pulls it into the plan
    oos = checked.out_of_scope[0].stop_id
    assert stops[int(oos[1:]) - 1].day_tag == DayTag.D1
    plan2, _ = assign_stop(EditContext(checked, stops, settings, matrix), oos, "T01", 1)
    checked2 = revalidate(EditContext(checked, stops, settings, matrix), plan2)
    assert oos in checked2.served_stop_ids and len(checked2.out_of_scope) == 1
