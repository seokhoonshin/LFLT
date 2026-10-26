"""Plain-English reasons for unassigned stops (brief 7.9), derived with the validator.

For each unassigned stop we try a cheapest insertion into every load of the plan (and as
a new load where a truck has a spare slot) using the timeline engine + rule checks:

* fails on volume everywhere            -> "Out of volume"
* passes volume but fails time everywhere -> "Out of time"
* some insertion is feasible            -> tier 1-3: "Not placed within the solve time ...";
                                           tier 4-5: "Not worth it" (pull-forward economics)
* a lower-tier stop is served and swapping it for this stop would be feasible
                                        -> "Priority anomaly" (solver bug signal)
"""

from __future__ import annotations

from dataclasses import dataclass

from routeopt.config import RunSettings
from routeopt.domain import Plan, PlanTruck, Stop, UnassignedStop
from routeopt.solver.plan_validator import check_truck
from routeopt.solver.timeline import RouteSkeleton, build_truck, node_index_map
from routeopt.travel.base import Matrix

REASON_VOLUME = "Out of volume"
REASON_TIME = "Out of time"
REASON_SOLVE_TIME = (
    "Not placed within the solve time — try a longer solve time, another preset, or more trucks"
)
REASON_NOT_WORTH = "Not worth it — adding this pull-forward stop would cost more than its value"
REASON_ANOMALY = "Priority anomaly"
CAPACITY_CODES = {"volume", "weight", "pieces"}
TIME_CODES = {"on_duty", "lunch", "reload", "too_many_loads"}

MAX_ATTEMPTS = 40_000


@dataclass
class InsertionTrial:
    truck: str
    load_seq: int
    position: int
    extra_minutes: float
    feasible: bool
    failed_on: str  # "", "volume", "time"
    new_load: bool = False


@dataclass
class ReasonResult:
    reason: str
    detail: str
    best: InsertionTrial | None = None
    anomaly_with: str | None = None


def _trial(
    truck: PlanTruck,
    loads: list[list[str]],
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> tuple[bool, str, PlanTruck]:
    skel = RouteSkeleton(truck_number=truck.number, driver_name=truck.driver_name, loads=loads)
    rebuilt = build_truck(skel, stops_by_id, matrix, node_of, settings)
    problems = [p for p in check_truck(rebuilt, stops_by_id, settings) if p.severity == "error"]
    if not problems:
        return True, "", rebuilt
    codes = {p.code for p in problems}
    if codes & CAPACITY_CODES:
        return False, "volume", rebuilt
    return False, "time", rebuilt


def attribute_reasons(
    plan: Plan, stops: list[Stop], settings: RunSettings, matrix: Matrix
) -> tuple[list[UnassignedStop], list[str]]:
    """Return the plan's unassigned list with reasons filled in, plus anomaly notes."""
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    anomalies: list[str] = []
    attempts = 0
    out: list[UnassignedStop] = []
    for u in plan.unassigned:
        stop = stops_by_id.get(u.stop_id)
        if stop is None:
            out.append(u)
            continue
        result, attempts = _reason_for(plan, stop, stops_by_id, matrix, node_of, settings, attempts)
        if result.reason == REASON_ANOMALY and result.anomaly_with:
            anomalies.append(
                f"{stop.stop_id} (tier {stop.tier}) was dropped while {result.anomaly_with} is served"
            )
        out.append(u.model_copy(update={"reason": result.reason, "detail": result.detail}))
    return out, anomalies


def _reason_for(
    plan: Plan,
    stop: Stop,
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
    attempts: int,
) -> tuple[ReasonResult, int]:
    trials: list[InsertionTrial] = []
    for truck in plan.trucks:
        base_loads = [[v.stop_id for v in load.stops] for load in truck.loads if load.stops]
        base_minutes = truck.on_duty_minutes
        # insert into existing loads
        for li, load in enumerate(base_loads):
            for pos in range(len(load) + 1):
                if attempts >= MAX_ATTEMPTS:
                    break
                attempts += 1
                loads = [list(x) for x in base_loads]
                loads[li].insert(pos, stop.stop_id)
                ok, failed_on, rebuilt = _trial(
                    truck, loads, stops_by_id, matrix, node_of, settings
                )
                trials.append(
                    InsertionTrial(
                        truck.number,
                        li + 1,
                        pos,
                        round(rebuilt.on_duty_minutes - base_minutes, 1),
                        ok,
                        failed_on,
                    )
                )
        # as a new load (spare slot), including on unused trucks
        if len(base_loads) < settings.max_loads_per_truck and attempts < MAX_ATTEMPTS:
            attempts += 1
            loads = [list(x) for x in base_loads] + [[stop.stop_id]]
            ok, failed_on, rebuilt = _trial(truck, loads, stops_by_id, matrix, node_of, settings)
            trials.append(
                InsertionTrial(
                    truck.number,
                    len(base_loads) + 1,
                    0,
                    round(rebuilt.on_duty_minutes - base_minutes, 1),
                    ok,
                    failed_on,
                    new_load=True,
                )
            )
    feasible = [t for t in trials if t.feasible]
    if feasible:
        best = min(feasible, key=lambda t: t.extra_minutes)
        where = (
            f"{best.truck} as a new load {best.load_seq}"
            if best.new_load
            else f"{best.truck} load {best.load_seq} at position {best.position + 1}"
        )
        detail = f"Fits on {where} (+{best.extra_minutes:.0f} min on duty)."
        if stop.tier >= 4:
            offset = stop.tier - 3
            value = settings.pull_forward_value_minutes.get(offset, 0)
            return (
                ReasonResult(
                    REASON_NOT_WORTH,
                    f"{detail} Pull-forward value is {value} min of driving.",
                    best,
                ),
                attempts,
            )
        return ReasonResult(REASON_SOLVE_TIME, detail, best), attempts
    # infeasible everywhere: anomaly check for D stops (tiers 1-3)
    if stop.tier <= 3:
        swap = _find_feasible_swap(plan, stop, stops_by_id, matrix, node_of, settings)
        if swap:
            return (
                ReasonResult(
                    REASON_ANOMALY,
                    f"Would fit if lower-priority stop {swap} were dropped instead.",
                    anomaly_with=swap,
                ),
                attempts,
            )
    if trials and all(t.failed_on == "volume" for t in trials):
        return (
            ReasonResult(
                REASON_VOLUME,
                f"Needs {stop.delivery_volume:.0f} cu ft delivered"
                + (f" and {stop.pickup_volume:.0f} cu ft picked up" if stop.pickup_volume else "")
                + "; no load has room at any point of its route.",
            ),
            attempts,
        )
    if trials:
        return (
            ReasonResult(
                REASON_TIME,
                f"Needs {stop.service_total_minutes} min on site; every truck would exceed "
                f"{settings.on_duty_hours:g} h on duty.",
            ),
            attempts,
        )
    return ReasonResult(REASON_TIME, "No truck is available."), attempts


def _find_feasible_swap(
    plan: Plan,
    stop: Stop,
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> str | None:
    """Return a served lower-priority stop id whose removal lets ``stop`` fit, if any."""
    budget = 5_000
    for truck in plan.trucks:
        base_loads = [[v.stop_id for v in load.stops] for load in truck.loads if load.stops]
        for li, load in enumerate(base_loads):
            for ri, other_id in enumerate(load):
                other = stops_by_id.get(other_id)
                if other is None or other.tier <= stop.tier:
                    continue
                for pos in range(len(load)):
                    if budget <= 0:
                        return None
                    budget -= 1
                    loads = [list(x) for x in base_loads]
                    loads[li].pop(ri)
                    loads[li].insert(pos, stop.stop_id)
                    ok, _f, _r = _trial(truck, loads, stops_by_id, matrix, node_of, settings)
                    if ok:
                        return other_id
    return None
