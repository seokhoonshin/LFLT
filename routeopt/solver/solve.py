"""Run the routing search and read the raw routes out of the assignment.

Search plan (all within ``solve_time_seconds`` wall-clock):

1. A slot-aware greedy start (:mod:`routeopt.solver.initial`) is handed to OR-Tools and
   improved with guided local search.  If the greedy start is rejected, OR-Tools' own
   first-solution heuristic runs instead.
2. Under Preset A (fixed cost per truck) the local search can leave a lightly used truck
   in place - GLS penalises arcs, not vehicle usage.  While time remains, we try to close
   the least-used truck: the model is rebuilt with that truck disabled, the search resumes
   from the current routes (the freed stops start unassigned) and the result is kept only
   if the objective improves.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ortools.constraint_solver import routing_enums_pb2

from routeopt.domain import SolverStats
from routeopt.solver.initial import greedy_routes
from routeopt.solver.model_builder import BuiltModel, SolverInputs, build_model

log = logging.getLogger(__name__)


def _status_names() -> dict[int, str]:
    try:
        items = routing_enums_pb2.RoutingSearchStatus.Value.items()  # type: ignore[attr-defined]
        return {int(code): str(name) for name, code in items}
    except AttributeError:  # pragma: no cover - older protobuf
        return {
            0: "ROUTING_NOT_SOLVED",
            1: "ROUTING_SUCCESS",
            2: "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
            3: "ROUTING_FAIL",
            4: "ROUTING_FAIL_TIMEOUT",
            5: "ROUTING_INVALID",
            6: "ROUTING_INFEASIBLE",
            7: "ROUTING_OPTIMAL",
        }


STATUS_NAMES: dict[int, str] = _status_names()

MIN_STAGE_SECONDS = 1.5
STAGE1_SHARE = 0.65
REDUCTION_SHARE = 0.2


@dataclass
class RawRoute:
    """One virtual vehicle's route: stop nodes in order plus solver times (seconds from shift start)."""

    vehicle: int
    truck_index: int
    slot: int
    nodes: list[int]
    arrivals: list[int]  # solver cumul (min) at each stop
    start_cumul: int
    end_cumul: int
    break_start: int | None = None  # relative to shift start, if a break is performed


@dataclass
class SolveOutcome:
    routes: list[RawRoute]
    dropped_nodes: list[int]
    stats: SolverStats
    assignment: Any = None
    built: BuiltModel | None = None
    solver_deliv_start: dict[int, int] = field(default_factory=dict)  # vehicle -> load-out tenths


def _read_breaks(built: BuiltModel, assignment: Any) -> dict[int, int]:
    """vehicle -> performed break start (relative seconds)."""
    out: dict[int, int] = {}
    if not built.breaks:
        return out
    name_to_vehicle = {f"lunch_v{v}": v for v in built.breaks}
    container = assignment.IntervalVarContainer()
    for i in range(container.Size()):
        element = container.Element(i)
        name = element.Var().Name()
        v = name_to_vehicle.get(name)
        if v is None:
            continue
        if element.PerformedValue():
            out[v] = int(element.StartValue())
    return out


def _set_limit(built: BuiltModel, seconds: float) -> None:
    built.search_parameters.time_limit.FromMilliseconds(max(200, int(seconds * 1000)))


def _routes_from_assignment(built: BuiltModel, assignment: Any) -> list[list[int]]:
    routing, manager = built.routing, built.manager
    out: list[list[int]] = []
    for v in range(routing.vehicles()):
        nodes: list[int] = []
        index = assignment.Value(routing.NextVar(routing.Start(v)))
        while not routing.IsEnd(index):
            nodes.append(manager.IndexToNode(index))
            index = assignment.Value(routing.NextVar(index))
        out.append(nodes)
    return out


def _search(
    built: BuiltModel, initial_routes: list[list[int]] | None, seconds: float
) -> tuple[Any, str]:
    """Run the search for ``seconds`` from ``initial_routes`` (or from scratch)."""
    routing = built.routing
    _set_limit(built, seconds)
    t0 = time.perf_counter()
    if initial_routes is not None:
        initial = routing.ReadAssignmentFromRoutes(initial_routes, True)
        if initial is not None:
            assignment = routing.SolveFromAssignmentWithParameters(initial, built.search_parameters)
            if assignment is not None:
                return assignment, "greedy"
        remaining = seconds - (time.perf_counter() - t0)
        if remaining < 0.2:
            return None, "greedy-rejected"
        _set_limit(built, remaining)
    return routing.SolveWithParameters(built.search_parameters), "heuristic"


def _extract_routes(
    built: BuiltModel, assignment: Any
) -> tuple[list[RawRoute], list[int], dict[int, int]]:
    routing, manager, time_dim = built.routing, built.manager, built.time_dim
    breaks = _read_breaks(built, assignment)
    routes: list[RawRoute] = []
    deliv_starts: dict[int, int] = {}
    visited: set[int] = set()
    for slot in built.slots:
        v = slot.vehicle
        index = routing.Start(v)
        start_cumul = int(assignment.Min(time_dim.CumulVar(index)))
        deliv_starts[v] = int(assignment.Min(built.deliv_dim.CumulVar(index)))
        nodes: list[int] = []
        arrivals: list[int] = []
        index = assignment.Value(routing.NextVar(index))
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            nodes.append(node)
            visited.add(node)
            arrivals.append(int(assignment.Min(time_dim.CumulVar(index))))
            index = assignment.Value(routing.NextVar(index))
        end_cumul = int(assignment.Min(time_dim.CumulVar(index)))
        routes.append(
            RawRoute(
                vehicle=v,
                truck_index=slot.truck_index,
                slot=slot.slot,
                nodes=nodes,
                arrivals=arrivals,
                start_cumul=start_cumul,
                end_cumul=end_cumul,
                break_start=breaks.get(v),
            )
        )
    dropped = [n for n in range(1, manager.GetNumberOfNodes()) if n not in visited]
    return routes, dropped, deliv_starts


def _least_used_truck(routes: list[list[int]], built: BuiltModel, disabled: set[int]) -> int | None:
    """Index of the used truck with the fewest stops (ties: lowest index)."""
    counts: dict[int, int] = {}
    for slot in built.slots:
        n = len(routes[slot.vehicle])
        if n:
            counts[slot.truck_index] = counts.get(slot.truck_index, 0) + n
    candidates = [(n, t) for t, n in counts.items() if t not in disabled]
    if len(candidates) < 2:
        return None
    return min(candidates)[1]


def solve(inputs: SolverInputs, use_initial: bool = True) -> SolveOutcome:
    """Build and solve (staged search).  Returns an outcome even when no solution was found
    (never raises for solver failures; raises ValueError for unusable settings)."""
    if not inputs.stops:
        return SolveOutcome(
            routes=[],
            dropped_nodes=[],
            stats=SolverStats(status="NO_STOPS", message="No stops to plan."),
        )
    settings = inputs.settings
    budget = float(settings.solve_time_seconds)
    t0 = time.perf_counter()
    built = build_model(inputs)
    reduce_fleet = settings.effective_truck_fixed_cost > 0 and len(inputs.trucks) >= 2

    initial_routes: list[list[int]] | None = None
    if use_initial:
        try:
            initial_routes = greedy_routes(inputs.stops, inputs.matrix, settings, inputs.trucks)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("greedy initial solution failed: %s", exc)
    stage1 = budget * STAGE1_SHARE if reduce_fleet else budget
    stage1 = max(MIN_STAGE_SECONDS, stage1 - (time.perf_counter() - t0))
    assignment, initial_used = _search(built, initial_routes, stage1)
    status_code = built.routing.status()
    notes: list[str] = []

    # ---- fleet reduction attempts (Preset A / custom fixed cost) ----------------------
    disabled: set[int] = set()
    if assignment is not None and reduce_fleet:
        best_obj = int(assignment.ObjectiveValue())
        best_routes = _routes_from_assignment(built, assignment)
        while True:
            remaining = budget - (time.perf_counter() - t0)
            if remaining < MIN_STAGE_SECONDS:
                break
            candidate = _least_used_truck(best_routes, built, disabled)
            if candidate is None:
                break
            trial_seconds = min(remaining, max(MIN_STAGE_SECONDS, budget * REDUCTION_SHARE))
            trial_built = build_model(inputs, disabled_trucks=disabled | {candidate})
            trial_routes = [
                [] if built.slots[v].truck_index == candidate else list(nodes)
                for v, nodes in enumerate(best_routes)
            ]
            trial_assignment, _how = _search(trial_built, trial_routes, trial_seconds)
            truck_number = inputs.trucks[candidate].truck_number
            if trial_assignment is not None and int(trial_assignment.ObjectiveValue()) < best_obj:
                best_obj = int(trial_assignment.ObjectiveValue())
                best_routes = _routes_from_assignment(trial_built, trial_assignment)
                built, assignment = trial_built, trial_assignment
                disabled.add(candidate)
                status_code = trial_built.routing.status()
                notes.append(f"closed {truck_number}")
            else:
                notes.append(f"kept {truck_number}")
                break

    # ---- polish: spend whatever time is left improving the best solution -------------
    remaining = budget - (time.perf_counter() - t0)
    if assignment is not None and remaining >= 1.0:
        polished, _how = _search(built, _routes_from_assignment(built, assignment), remaining)
        if polished is not None and int(polished.ObjectiveValue()) <= int(
            assignment.ObjectiveValue()
        ):
            assignment = polished
            status_code = built.routing.status()

    wall = time.perf_counter() - t0
    status = STATUS_NAMES.get(status_code, str(status_code))
    stats = SolverStats(
        status=status,
        status_code=status_code,
        wall_time_s=round(wall, 2),
        first_solution_found=assignment is not None,
        first_solution_strategy=settings.first_solution_strategy,
        local_search="GUIDED_LOCAL_SEARCH",
        nodes=built.manager.GetNumberOfNodes(),
        virtual_vehicles=built.routing.vehicles(),
        lunch_mode=built.lunch_mode,
        initial_solution=initial_used,
        fleet_reduction="; ".join(notes) if notes else ("no time left" if reduce_fleet else "off"),
    )
    if assignment is None:
        stats.message = (
            "The optimizer could not find any plan within the time limit. Try a longer "
            "solve time, fewer stops, or check the settings."
        )
        log.warning("solver: no solution (%s) after %.1fs", status, wall)
        return SolveOutcome(routes=[], dropped_nodes=[], stats=stats, built=built)

    stats.objective = int(assignment.ObjectiveValue())
    routes, dropped, deliv_starts = _extract_routes(built, assignment)
    stats.vehicles_used = len({r.truck_index for r in routes if r.nodes})
    log.info(
        "solver: %s objective=%s wall=%.1fs trucks=%s dropped=%s fleet=%s",
        status,
        stats.objective,
        wall,
        stats.vehicles_used,
        len(dropped),
        stats.fleet_reduction,
    )
    return SolveOutcome(
        routes=routes,
        dropped_nodes=dropped,
        stats=stats,
        assignment=assignment,
        built=built,
        solver_deliv_start=deliv_starts,
    )


def solve_model(built: BuiltModel, inputs: SolverInputs, use_initial: bool = True) -> SolveOutcome:
    """Compatibility wrapper: solve a pre-built model in one stage (no fleet reduction)."""
    t0 = time.perf_counter()
    initial_routes = None
    if use_initial:
        initial_routes = greedy_routes(inputs.stops, inputs.matrix, inputs.settings, inputs.trucks)
    assignment, initial_used = _search(
        built, initial_routes, float(inputs.settings.solve_time_seconds)
    )
    wall = time.perf_counter() - t0
    status_code = built.routing.status()
    stats = SolverStats(
        status=STATUS_NAMES.get(status_code, str(status_code)),
        status_code=status_code,
        wall_time_s=round(wall, 2),
        first_solution_found=assignment is not None,
        first_solution_strategy=inputs.settings.first_solution_strategy,
        local_search="GUIDED_LOCAL_SEARCH",
        nodes=built.manager.GetNumberOfNodes(),
        virtual_vehicles=built.routing.vehicles(),
        lunch_mode=built.lunch_mode,
        initial_solution=initial_used,
        fleet_reduction="off",
    )
    if assignment is None:
        stats.message = "The optimizer could not find any plan within the time limit."
        return SolveOutcome(routes=[], dropped_nodes=[], stats=stats, built=built)
    stats.objective = int(assignment.ObjectiveValue())
    routes, dropped, deliv_starts = _extract_routes(built, assignment)
    stats.vehicles_used = len({r.truck_index for r in routes if r.nodes})
    return SolveOutcome(
        routes=routes,
        dropped_nodes=dropped,
        stats=stats,
        assignment=assignment,
        built=built,
        solver_deliv_start=deliv_starts,
    )
