"""Turn raw solver routes into a Plan (trucks -> loads -> ordered stop visits)."""

from __future__ import annotations

import uuid

from routeopt.config import RunSettings
from routeopt.domain import LunchInfo, Plan, PlanTruck, Stop, UnassignedStop, hub_now
from routeopt.solver.kpis import compute_kpis
from routeopt.solver.model_builder import SolverInputs
from routeopt.solver.solve import RawRoute, SolveOutcome
from routeopt.solver.timeline import (
    RouteSkeleton,
    build_truck,
    node_index_map,
    skeleton_from_truck,
)
from routeopt.travel.base import Matrix


def new_run_id() -> str:
    return hub_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _locate_break(
    loads: list[RawRoute],
    break_vehicle: int,
    break_start: int,
    service_s: list[int],
    settings: RunSettings,
) -> tuple[int, int]:
    """(load_seq, after_stop_seq) of the arc that contains a break starting at break_start.

    The break can only live on its own vehicle's route; we pick the last point (hub
    departure or stop departure) at or before the break start.
    """
    for load_seq, route in enumerate(loads, start=1):
        if route.vehicle != break_vehicle:
            continue
        departs = [route.start_cumul + (settings.reload_seconds if route.slot > 1 else 0)]
        for node, arrive in zip(route.nodes, route.arrivals, strict=True):
            departs.append(arrive + service_s[node])
        after = 0
        for k, d in enumerate(departs):
            if d <= break_start:
                after = k
        return load_seq, after
    return 1, 0


def extract_plan(
    outcome: SolveOutcome,
    inputs: SolverInputs,
    run_id: str | None = None,
    matrix_source: str | None = None,
) -> Plan:
    """Build the Plan object (version 0) from a solve outcome."""
    settings = inputs.settings
    stops = inputs.stops
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    shift = settings.shift_start_s
    service_s = [0] + [s.service_total_minutes * 60 for s in stops]

    routes_by_truck: dict[int, list[RawRoute]] = {}
    for r in outcome.routes:
        if r.nodes:
            routes_by_truck.setdefault(r.truck_index, []).append(r)

    trucks: list[PlanTruck] = []
    for t_idx, truck in enumerate(inputs.trucks):
        routes = sorted(routes_by_truck.get(t_idx, []), key=lambda r: r.slot)
        if not routes:
            trucks.append(PlanTruck(number=truck.truck_number, driver_name=truck.driver_name))
            continue
        loads = [[stops[n - 1].stop_id for n in r.nodes] for r in routes]
        # smart mode: lunch is a time budget in the model; the timeline engine places it
        # (lunch=None).  simple mode: the solver placed an explicit break interval.
        lunch: LunchInfo | None = None
        for r in routes:
            if r.break_start is not None:
                load_seq, after = _locate_break(
                    routes, r.vehicle, r.break_start, service_s, settings
                )
                lunch = LunchInfo(
                    kind="break",
                    load_seq=load_seq,
                    after_stop_seq=after,
                    start_s=shift + r.break_start,
                )
                break
        skeleton = RouteSkeleton(
            truck_number=truck.truck_number,
            driver_name=truck.driver_name,
            loads=loads,
            lunch=lunch,
            solver_arrivals={
                stops[n - 1].stop_id: shift + a
                for r in routes
                for n, a in zip(r.nodes, r.arrivals, strict=True)
            },
            solver_returns=[shift + r.end_cumul for r in routes],
        )
        trucks.append(build_truck(skeleton, stops_by_id, inputs.matrix, node_of, settings))

    unassigned = [_unassigned(stops[n - 1]) for n in sorted(outcome.dropped_nodes)]
    plan = Plan(
        run_id=run_id or new_run_id(),
        plan_version=0,
        plan_date=settings.plan_date,
        created_at=hub_now(),
        settings=settings.model_dump(mode="json"),
        matrix_source=matrix_source or inputs.matrix.source,
        solver_stats=outcome.stats,
        trucks=trucks,
        unassigned=unassigned,
        warnings=list(inputs.matrix.notes),
    )
    plan.kpis = compute_kpis(plan, stops, settings, len(inputs.trucks))
    return plan


def _unassigned(stop: Stop, reason: str = "") -> UnassignedStop:
    return UnassignedStop(
        stop_id=stop.stop_id,
        tier=stop.tier,
        day_tag=stop.day_tag,
        volume=stop.delivery_volume,
        pickup_volume=stop.pickup_volume,
        service_minutes=stop.service_total_minutes,
        reason=reason,
    )


def rebuild_plan_trucks(
    plan: Plan,
    stops: list[Stop],
    settings: RunSettings,
    matrix: Matrix,
    keep_solver_times: bool = False,
) -> Plan:
    """Recompute every truck's timeline from the plan's structure (editor / re-validation)."""
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    new_trucks: list[PlanTruck] = []
    for truck in plan.trucks:
        skel = skeleton_from_truck(truck)
        if not keep_solver_times:
            # after an edit the structure changed: drop solver times and let the timeline
            # engine place lunch again by rule
            skel.solver_arrivals = None
            skel.solver_returns = None
            skel.lunch = None
        new_trucks.append(build_truck(skel, stops_by_id, matrix, node_of, settings))
    served = {v.stop_id for t in new_trucks for load in t.loads for v in load.stops}
    unassigned = [u for u in plan.unassigned if u.stop_id not in served]
    out_of_scope = [u for u in plan.out_of_scope if u.stop_id not in served]
    known = {u.stop_id for u in unassigned} | {u.stop_id for u in out_of_scope} | served
    for s in stops:
        if s.stop_id not in known:
            unassigned.append(_unassigned(s, "Removed from a route by the planner"))
    new_plan = plan.model_copy(
        update={"trucks": new_trucks, "unassigned": unassigned, "out_of_scope": out_of_scope}
    )
    new_plan.kpis = compute_kpis(new_plan, stops, settings, len(plan.trucks))
    return new_plan
