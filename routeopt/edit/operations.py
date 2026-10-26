"""Manual plan edits (brief section 10): move / re-sequence / unassign / assign.

Every operation is pure: it takes an :class:`EditContext` and returns a *new* Plan whose
structure (which stop ids sit in which load, in which order) changed, together with an
:class:`EditLogEntry` describing the change.  Times, volumes, KPIs and violations are
recomputed afterwards by :func:`routeopt.edit.revalidate.revalidate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from routeopt.config import RunSettings
from routeopt.db.repo import EditLogEntry
from routeopt.domain import Load, Plan, PlanTruck, Stop, StopVisit, UnassignedStop
from routeopt.travel.base import Matrix

REMOVED_BY_PLANNER = "Removed from a route by the planner"


class EditError(ValueError):
    """A planner-facing problem with an edit request."""


@dataclass
class EditContext:
    plan: Plan
    stops: list[Stop]
    settings: RunSettings
    matrix: Matrix

    @property
    def stops_by_id(self) -> dict[str, Stop]:
        return {s.stop_id: s for s in self.stops}


@dataclass
class Location:
    truck: str
    load_seq: int
    position: int  # 0-based

    def as_dict(self) -> dict[str, Any]:
        return {"truck": self.truck, "load": self.load_seq, "position": self.position + 1}


def _find_stop(plan: Plan, stop_id: str) -> Location | None:
    for truck in plan.trucks:
        for load in truck.loads:
            for i, visit in enumerate(load.stops):
                if visit.stop_id == stop_id:
                    return Location(truck.number, load.seq, i)
    return None


def _truck(plan: Plan, number: str) -> PlanTruck:
    try:
        return plan.truck(number)
    except KeyError:
        raise EditError(f"Truck {number} is not part of this plan.") from None


def _placeholder_visit(stop_id: str) -> StopVisit:
    """A structural visit; every time/volume field is recomputed by revalidate()."""
    return StopVisit(
        seq=0,
        stop_id=stop_id,
        arrive_s=0,
        depart_s=0,
        travel_min_from_prev=0.0,
        dist_mi_from_prev=0.0,
        volume_on_arrival=0.0,
        volume_after=0.0,
        eta_window_start_s=0,
        eta_window_end_s=0,
    )


def _normalize_truck(truck: PlanTruck) -> None:
    """Drop empty loads, renumber loads 1..n and visits 1..m."""
    truck.loads = [load for load in truck.loads if load.stops]
    for li, load in enumerate(truck.loads, start=1):
        load.seq = li
        for vi, visit in enumerate(load.stops, start=1):
            visit.seq = vi


def _remove_everywhere(plan: Plan, stop_id: str) -> Location | None:
    """Remove the stop from any load / unassigned / out-of-scope list."""
    where = _find_stop(plan, stop_id)
    if where is not None:
        truck = _truck(plan, where.truck)
        load = next(load for load in truck.loads if load.seq == where.load_seq)
        load.stops = [v for v in load.stops if v.stop_id != stop_id]
        _normalize_truck(truck)
    plan.unassigned = [u for u in plan.unassigned if u.stop_id != stop_id]
    plan.out_of_scope = [u for u in plan.out_of_scope if u.stop_id != stop_id]
    return where


def _insert(
    plan: Plan, stop_id: str, truck_number: str, load_seq: int, position: int | None, max_loads: int
) -> Location:
    truck = _truck(plan, truck_number)
    loads = [load for load in truck.loads if load.stops]
    if load_seq == len(loads) + 1:
        if len(loads) >= max_loads:
            raise EditError(
                f"Truck {truck_number} already has {len(loads)} loads (the limit is {max_loads})."
            )
        new_load = Load(
            seq=load_seq, depart_s=0, return_s=0, load_out_volume=0.0, max_volume_on_road=0.0
        )
        truck.loads = [*loads, new_load]
        target = new_load
    elif 1 <= load_seq <= len(loads):
        truck.loads = loads
        target = loads[load_seq - 1]
    else:
        raise EditError(
            f"Truck {truck_number} has {len(loads)} loads; choose load 1-{len(loads)} or "
            f"{len(loads) + 1} to start a new load."
        )
    pos = len(target.stops) if position is None else max(0, min(position, len(target.stops)))
    target.stops.insert(pos, _placeholder_visit(stop_id))
    _normalize_truck(truck)
    return Location(truck_number, target.seq, pos)


def _entry(operation: str, before: dict[str, Any], after: dict[str, Any], who: str) -> EditLogEntry:
    return EditLogEntry(who=who, operation=operation, before=before, after=after)


def move_stop(
    ctx: EditContext,
    stop_id: str,
    to_truck: str,
    to_load_seq: int,
    position: int | None = None,
    who: str = "planner",
) -> tuple[Plan, EditLogEntry]:
    """Move a stop to another load (or a new load on a truck with a free slot)."""
    if stop_id not in ctx.stops_by_id:
        raise EditError(f"Stop {stop_id} is not part of this run.")
    plan = ctx.plan.model_copy(deep=True)
    before = _find_stop(plan, stop_id)
    if before is None:
        raise EditError(f"Stop {stop_id} is not on any route; use 'assign' instead.")
    _remove_everywhere(plan, stop_id)
    after = _insert(
        plan, stop_id, to_truck, to_load_seq, position, ctx.settings.max_loads_per_truck
    )
    entry = _entry(
        "move",
        {"stop_id": stop_id, **before.as_dict()},
        {"stop_id": stop_id, **after.as_dict()},
        who,
    )
    return plan, entry


def reorder_stop(
    ctx: EditContext,
    truck: str,
    load_seq: int,
    stop_id: str,
    new_position: int,
    who: str = "planner",
) -> tuple[Plan, EditLogEntry]:
    """Change a stop's position within its load (0-based new_position)."""
    plan = ctx.plan.model_copy(deep=True)
    where = _find_stop(plan, stop_id)
    if where is None or where.truck != truck or where.load_seq != load_seq:
        raise EditError(f"Stop {stop_id} is not on truck {truck} load {load_seq}.")
    load = next(load for load in _truck(plan, truck).loads if load.seq == load_seq)
    visit = load.stops.pop(where.position)
    pos = max(0, min(new_position, len(load.stops)))
    load.stops.insert(pos, visit)
    _normalize_truck(_truck(plan, truck))
    entry = _entry(
        "reorder",
        {"stop_id": stop_id, **where.as_dict()},
        {"stop_id": stop_id, **Location(truck, load_seq, pos).as_dict()},
        who,
    )
    return plan, entry


def move_up(ctx: EditContext, truck: str, load_seq: int, stop_id: str) -> tuple[Plan, EditLogEntry]:
    where = _find_stop(ctx.plan, stop_id)
    if where is None:
        raise EditError(f"Stop {stop_id} is not on any route.")
    return reorder_stop(ctx, truck, load_seq, stop_id, max(0, where.position - 1))


def move_down(
    ctx: EditContext, truck: str, load_seq: int, stop_id: str
) -> tuple[Plan, EditLogEntry]:
    where = _find_stop(ctx.plan, stop_id)
    if where is None:
        raise EditError(f"Stop {stop_id} is not on any route.")
    return reorder_stop(ctx, truck, load_seq, stop_id, where.position + 1)


def unassign_stop(
    ctx: EditContext, stop_id: str, who: str = "planner"
) -> tuple[Plan, EditLogEntry]:
    """Take a stop off its route; it is listed as unassigned with a planner reason."""
    stop = ctx.stops_by_id.get(stop_id)
    if stop is None:
        raise EditError(f"Stop {stop_id} is not part of this run.")
    plan = ctx.plan.model_copy(deep=True)
    where = _remove_everywhere(plan, stop_id)
    if where is None:
        raise EditError(f"Stop {stop_id} is not on any route.")
    plan.unassigned.append(
        UnassignedStop(
            stop_id=stop_id,
            tier=stop.tier,
            day_tag=stop.day_tag,
            volume=stop.delivery_volume,
            pickup_volume=stop.pickup_volume,
            service_minutes=stop.service_total_minutes,
            reason=REMOVED_BY_PLANNER,
        )
    )
    entry = _entry(
        "unassign",
        {"stop_id": stop_id, **where.as_dict()},
        {"stop_id": stop_id, "unassigned": True},
        who,
    )
    return plan, entry


def assign_stop(
    ctx: EditContext,
    stop_id: str,
    to_truck: str,
    to_load_seq: int,
    position: int | None = None,
    who: str = "planner",
) -> tuple[Plan, EditLogEntry]:
    """Put an unassigned (or not-in-scope) stop onto a load."""
    if stop_id not in ctx.stops_by_id:
        raise EditError(f"Stop {stop_id} is not part of this run.")
    if _find_stop(ctx.plan, stop_id) is not None:
        raise EditError(f"Stop {stop_id} is already on a route; use 'move' instead.")
    plan = ctx.plan.model_copy(deep=True)
    _remove_everywhere(plan, stop_id)
    after = _insert(
        plan, stop_id, to_truck, to_load_seq, position, ctx.settings.max_loads_per_truck
    )
    entry = _entry(
        "assign",
        {"stop_id": stop_id, "unassigned": True},
        {"stop_id": stop_id, **after.as_dict()},
        who,
    )
    return plan, entry


def locate(plan: Plan, stop_id: str) -> Location | None:
    """Where a stop currently sits (None when unassigned)."""
    return _find_stop(plan, stop_id)
