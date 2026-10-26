"""Independent post-solve validator - the source of truth for every exported plan (7.8).

Pure Python; knows nothing about OR-Tools.  It rebuilds each truck's timeline and volume
profile from the plan's *structure* with the shared timeline engine, checks every hard
rule of brief section 3, cross-checks the solver's own times (drift > tolerance is a
warning) and returns a list of :class:`Violation` (empty = valid).
"""

from __future__ import annotations

from routeopt.config import RunSettings
from routeopt.domain import Plan, PlanTruck, Stop, Violation, clock
from routeopt.solver.timeline import (
    RouteSkeleton,
    build_truck,
    lunch_via_reload,
    node_index_map,
    skeleton_from_truck,
)
from routeopt.travel.base import Matrix

EPS = 1e-6


def check_truck(
    truck: PlanTruck,
    stops_by_id: dict[str, Stop],
    settings: RunSettings,
    drift_tolerance_s: int | None = None,
) -> list[Violation]:
    """Rule checks for one (already rebuilt) truck timeline."""
    v: list[Violation] = []
    number = truck.number
    if not truck.used:
        return v
    loads = [load for load in truck.loads if load.stops]
    cap = settings.truck_volume_cuft
    if len(loads) > settings.max_loads_per_truck:
        v.append(
            Violation(
                severity="error",
                code="too_many_loads",
                truck=number,
                message=f"{len(loads)} loads exceed the limit of {settings.max_loads_per_truck} per truck.",
            )
        )
    limit_min = settings.on_duty_hours * 60
    if truck.on_duty_minutes > limit_min + EPS:
        v.append(
            Violation(
                severity="error",
                code="on_duty",
                truck=number,
                message=(
                    f"On duty {truck.on_duty_minutes:.0f} min (returns {clock(loads[-1].return_s)}) "
                    f"exceeds the {limit_min:.0f}-min limit."
                ),
            )
        )
    for load in loads:
        if load.load_out_volume > cap + EPS:
            v.append(
                Violation(
                    severity="error",
                    code="volume",
                    truck=number,
                    load=load.seq,
                    message=f"Load-out {load.load_out_volume:.0f} cu ft exceeds the truck's {cap:.0f} cu ft.",
                )
            )
        for visit in load.stops:
            if visit.volume_after > cap + EPS:
                v.append(
                    Violation(
                        severity="error",
                        code="volume",
                        truck=number,
                        load=load.seq,
                        stop_id=visit.stop_id,
                        message=(
                            f"After stop {visit.seq} the truck would carry {visit.volume_after:.0f} cu ft "
                            f"(max {cap:.0f}); a return was picked up before there was room."
                        ),
                    )
                )
            if visit.volume_after < -EPS or visit.volume_on_arrival < -EPS:
                v.append(
                    Violation(
                        severity="error",
                        code="volume",
                        truck=number,
                        load=load.seq,
                        stop_id=visit.stop_id,
                        message=f"Volume goes negative at stop {visit.seq} (delivering more than was loaded).",
                    )
                )
            if settings.truck_weight_lb_cap and (
                visit.weight_after > settings.truck_weight_lb_cap + EPS
                or load.load_out_weight > settings.truck_weight_lb_cap + EPS
            ):
                v.append(
                    Violation(
                        severity="error",
                        code="weight",
                        truck=number,
                        load=load.seq,
                        stop_id=visit.stop_id,
                        message=f"Weight exceeds the {settings.truck_weight_lb_cap:.0f} lb cap at stop {visit.seq}.",
                    )
                )
            if settings.truck_pieces_cap and (
                visit.pieces_after > settings.truck_pieces_cap
                or load.load_out_pieces > settings.truck_pieces_cap
            ):
                v.append(
                    Violation(
                        severity="error",
                        code="pieces",
                        truck=number,
                        load=load.seq,
                        stop_id=visit.stop_id,
                        message=f"Pieces exceed the cap of {settings.truck_pieces_cap} at stop {visit.seq}.",
                    )
                )
        if (
            load.seq >= 2
            and load.reload_start_s is not None
            and load.depart_s - load.reload_start_s < settings.reload_seconds - 1
        ):
            v.append(
                Violation(
                    severity="error",
                    code="reload",
                    truck=number,
                    load=load.seq,
                    message=f"Load {load.seq} departs before the {settings.reload_minutes}-min reload is over.",
                )
            )
    # ---- lunch --------------------------------------------------------------------
    lunch = truck.lunch
    lunch_ok = False
    if lunch.kind == "reload" and len(loads) >= 2 and lunch_via_reload(settings):
        lunch_ok = True
    elif lunch.kind == "break" and lunch.start_s is not None and lunch.end_s is not None:
        duration_ok = lunch.end_s - lunch.start_s >= settings.lunch_seconds - 1
        inside_day = (
            lunch.start_s >= settings.shift_start_s and lunch.end_s <= loads[-1].return_s + 1
        )
        overlaps = any(
            visit.arrive_s < lunch.end_s and lunch.start_s < visit.depart_s
            for load in loads
            for visit in load.stops
        )
        lunch_ok = duration_ok and inside_day and not overlaps
        if overlaps:
            v.append(
                Violation(
                    severity="error",
                    code="lunch",
                    truck=number,
                    message="Lunch overlaps service at a stop.",
                )
            )
        earliest, latest = settings.lunch_window_rel()
        if lunch_ok and (settings.lunch_earliest is not None or settings.lunch_latest is not None):
            rel = lunch.start_s - settings.shift_start_s
            if rel < earliest - 1 or rel > latest + 1:
                v.append(
                    Violation(
                        severity="warning",
                        code="lunch_window",
                        truck=number,
                        message=(
                            f"Lunch starts at {clock(lunch.start_s)}, outside the preferred window "
                            f"{clock(settings.shift_start_s + earliest)}-{clock(settings.shift_start_s + latest)}."
                        ),
                    )
                )
    if not lunch_ok and settings.lunch_minutes > 0:
        v.append(
            Violation(
                severity="error",
                code="lunch",
                truck=number,
                message=(
                    f"No {settings.lunch_minutes}-min lunch: the driver needs a gap with no service "
                    "or a second load (lunch during the reload)."
                ),
            )
        )
    # ---- drift vs solver ---------------------------------------------------------
    tol = settings.time_drift_tolerance_s if drift_tolerance_s is None else drift_tolerance_s
    for load in loads:
        for visit in load.stops:
            if visit.solver_arrive_s is None:
                continue
            expected = _solver_equivalent(truck, settings, load.seq, visit.arrive_s, visit.seq)
            if abs(expected - visit.solver_arrive_s) > tol:
                v.append(
                    Violation(
                        severity="warning",
                        code="drift",
                        truck=number,
                        load=load.seq,
                        stop_id=visit.stop_id,
                        message=(
                            f"Rebuilt ETA {clock(visit.arrive_s)} differs from the optimizer's "
                            f"{clock(visit.solver_arrive_s)} by more than {tol} s."
                        ),
                    )
                )
        if load.solver_return_s is not None:
            expected = _solver_equivalent(truck, settings, load.seq, load.return_s, None)
            if abs(expected - load.solver_return_s) > tol:
                v.append(
                    Violation(
                        severity="warning",
                        code="drift",
                        truck=number,
                        load=load.seq,
                        message=(
                            f"Rebuilt return {clock(load.return_s)} differs from the optimizer's "
                            f"{clock(load.solver_return_s)} by more than {tol} s."
                        ),
                    )
                )
    return v


def _solver_equivalent(
    truck: PlanTruck, settings: RunSettings, load_seq: int, t: int, visit_seq: int | None
) -> int:
    """Translate a rebuilt time into what the solver's own timeline should show.

    In ``smart`` mode the solver budgets the lunch on load 1's return arc (and takes it
    back out of the reload when the reload can host lunch) while the timeline engine
    places the actual gap; this maps between the two so drift can be compared.
    ``visit_seq`` is None for a load's return time.
    """
    if settings.lunch_mode != "smart" or settings.lunch_seconds <= 0:
        return t
    lunch_s = settings.lunch_seconds
    lunch = truck.lunch
    is_return = visit_seq is None
    if lunch.kind == "reload":
        return t + (lunch_s if is_return and load_seq == 1 else 0)
    if lunch.kind == "break" and lunch.load_seq is not None and lunch.after_stop_seq is not None:
        if lunch.load_seq < load_seq:
            taken = True
        elif lunch.load_seq == load_seq:
            taken = is_return or lunch.after_stop_seq < (visit_seq or 0)
        else:
            taken = False
        budget = load_seq >= 2 or is_return
        return t - (lunch_s if taken else 0) + (lunch_s if budget else 0)
    return t


def validate_plan(
    plan: Plan, stops: list[Stop], settings: RunSettings, matrix: Matrix
) -> list[Violation]:
    """Rebuild every truck from the plan's structure and check all rules.

    ``stops`` must be in matrix node order (node i+1 = stops[i]).
    """
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    violations: list[Violation] = []
    seen: dict[str, str] = {}
    for truck in plan.trucks:
        for load in truck.loads:
            for visit in load.stops:
                if visit.stop_id not in stops_by_id:
                    violations.append(
                        Violation(
                            severity="error",
                            code="unknown_stop",
                            truck=truck.number,
                            load=load.seq,
                            stop_id=visit.stop_id,
                            message="Stop is not part of this run's input.",
                        )
                    )
                    continue
                if visit.stop_id in seen:
                    violations.append(
                        Violation(
                            severity="error",
                            code="duplicate_stop",
                            truck=truck.number,
                            load=load.seq,
                            stop_id=visit.stop_id,
                            message=f"Stop is also on {seen[visit.stop_id]}; a stop is served by one load only.",
                        )
                    )
                seen[visit.stop_id] = f"truck {truck.number} load {load.seq}"
    served = set(seen)
    for u in plan.unassigned:
        if u.stop_id in served:
            violations.append(
                Violation(
                    severity="error",
                    code="unassigned_served",
                    stop_id=u.stop_id,
                    message="Stop is listed as unassigned but also appears on a route.",
                )
            )
    for truck in plan.trucks:
        if not truck.used:
            continue
        skel: RouteSkeleton = skeleton_from_truck(truck)
        if any(sid not in stops_by_id for load in skel.loads for sid in load):
            continue
        rebuilt = build_truck(skel, stops_by_id, matrix, node_of, settings)
        violations.extend(check_truck(rebuilt, stops_by_id, settings))
        # stale-plan check: stored times must match the rebuilt ones
        for stored, fresh in zip(truck.loads, rebuilt.loads, strict=False):
            if abs(stored.return_s - fresh.return_s) > 1:
                violations.append(
                    Violation(
                        severity="warning",
                        code="stale",
                        truck=truck.number,
                        load=stored.seq,
                        message="Stored times are out of date; the plan was recomputed.",
                    )
                )
                break
    return violations
