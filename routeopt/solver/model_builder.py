"""Build the OR-Tools routing model (brief section 7).

Nodes: 0 = hub, 1..S = stops.  Vehicles: one *virtual vehicle* per (truck, load slot).
Dimensions: ``time`` (travel + service at the from-node, reload on slot >= 2 start arcs),
``deliv`` (decreasing from a free load-out to 0 at the hub) and ``pick`` (increasing from
0), with ``deliv + pick <= cap`` on every arrival and every route end (VRPSPD).
Stops are optional through disjunctions whose penalties encode the tier ordering.
Lunch is modelled with optional break intervals ("smart") or a mandatory break on slot 1
("simple").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from routeopt.config import RunSettings, validate_penalty_invariants
from routeopt.domain import Stop, TruckConfig
from routeopt.solver.timeline import lunch_via_reload
from routeopt.travel.base import Matrix


@dataclass
class SolverInputs:
    stops: list[Stop]  # node i+1
    matrix: Matrix  # over [hub] + stops
    settings: RunSettings
    trucks: list[TruckConfig]  # selected trucks, in order

    def __post_init__(self) -> None:
        if self.matrix.n != len(self.stops) + 1:
            raise ValueError("matrix size must equal number of stops + 1 (hub)")


@dataclass
class VehicleSlot:
    vehicle: int
    truck_index: int
    slot: int  # 1..K


@dataclass
class BuiltModel:
    manager: pywrapcp.RoutingIndexManager
    routing: pywrapcp.RoutingModel
    time_dim: pywrapcp.RoutingDimension
    deliv_dim: pywrapcp.RoutingDimension
    pick_dim: pywrapcp.RoutingDimension
    slots: list[VehicleSlot]
    breaks: dict[int, Any]  # vehicle -> IntervalVar
    node_service_s: list[int]  # per node (0 = hub)
    penalties: list[int]  # per stop (node i+1)
    search_parameters: Any
    lunch_mode: str
    extra_dims: dict[str, tuple[pywrapcp.RoutingDimension, pywrapcp.RoutingDimension]] = field(
        default_factory=dict
    )

    def vehicle_of(self, truck_index: int, slot: int) -> int:
        return truck_index * self.slots_per_truck + (slot - 1)

    @property
    def slots_per_truck(self) -> int:
        return max(s.slot for s in self.slots) if self.slots else 1


def _tenths(value: float) -> int:
    return round(value * 10)


def build_model(inputs: SolverInputs, disabled_trucks: set[int] | None = None) -> BuiltModel:
    """Create the routing model with all constraints and the search parameters.

    ``disabled_trucks`` (indices into ``inputs.trucks``) are kept in the vehicle numbering
    but forced empty - used by the fleet-reduction stage of the search.
    """
    settings = inputs.settings
    problems = validate_penalty_invariants(settings, len(inputs.trucks))
    if problems:
        raise ValueError("Settings are not usable: " + " ".join(problems))
    if not inputs.trucks:
        raise ValueError("Select at least one truck.")

    stops = inputs.stops
    n_nodes = len(stops) + 1
    k_slots = settings.max_loads_per_truck
    n_trucks = len(inputs.trucks)
    n_vehicles = n_trucks * k_slots
    horizon = settings.on_duty_seconds
    cap = settings.volume_cap_tenths

    manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)
    solver = routing.solver()

    # ---- transit matrices ------------------------------------------------------------
    # transit(i, j) = travel + service at the from-node.  Slot 1's return arc carries the
    # lunch budget in "smart" mode; slot >= 2 start arcs carry the reload (minus the lunch
    # already budgeted when the reload is long enough to host it) - see _lunch_budget().
    service_s = [0] + [s.service_total_minutes * 60 for s in stops]
    travel = inputs.matrix.durations_s
    lunch_on_return, reload_extra = _lunch_budget(settings, k_slots)
    time_slot1 = [[travel[i][j] + service_s[i] for j in range(n_nodes)] for i in range(n_nodes)]
    time_slotk = [row[:] for row in time_slot1]
    for i in range(1, n_nodes):  # stop -> hub on slot 1: lunch budget (smart mode)
        time_slot1[i][0] += lunch_on_return
    for j in range(1, n_nodes):  # reload on Start -> stop arcs; Start -> End stays 0
        time_slotk[0][j] += reload_extra

    travel_cb = routing.RegisterTransitMatrix(travel)
    routing.SetArcCostEvaluatorOfAllVehicles(travel_cb)
    cb_slot1 = routing.RegisterTransitMatrix(time_slot1)
    cb_slotk = routing.RegisterTransitMatrix(time_slotk)

    slots: list[VehicleSlot] = []
    time_cbs: list[int] = []
    for t in range(n_trucks):
        for k in range(1, k_slots + 1):
            v = t * k_slots + (k - 1)
            slots.append(VehicleSlot(vehicle=v, truck_index=t, slot=k))
            time_cbs.append(cb_slot1 if k == 1 else cb_slotk)

    # ---- time dimension --------------------------------------------------------------
    routing.AddDimensionWithVehicleTransits(time_cbs, horizon, horizon, False, "time")
    time_dim = routing.GetDimensionOrDie("time")
    for s in slots:
        start = routing.Start(s.vehicle)
        end = routing.End(s.vehicle)
        if s.slot == 1:
            time_dim.CumulVar(start).SetRange(0, 0)
        else:
            prev = routing.End(s.vehicle - 1)
            solver.Add(time_dim.CumulVar(start) >= time_dim.CumulVar(prev))
            # symmetry breaking: slot k+1 can only be active if slot k is
            solver.Add(
                routing.ActiveVehicleVar(s.vehicle) <= routing.ActiveVehicleVar(s.vehicle - 1)
            )
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(start))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(end))
        if disabled_trucks and s.truck_index in disabled_trucks:
            solver.Add(routing.ActiveVehicleVar(s.vehicle) == 0)

    # ---- capacity: deliveries and pickups (VRPSPD) -----------------------------------
    deliv_tenths = [0] + [_tenths(s.delivery_volume) for s in stops]
    pick_tenths = [0] + [_tenths(s.pickup_volume) for s in stops]
    deliv_cb = routing.RegisterUnaryTransitVector([-x for x in deliv_tenths])
    pick_cb = routing.RegisterUnaryTransitVector(pick_tenths)
    routing.AddDimension(deliv_cb, 0, cap, False, "deliv")
    routing.AddDimension(pick_cb, 0, cap, True, "pick")
    deliv_dim = routing.GetDimensionOrDie("deliv")
    pick_dim = routing.GetDimensionOrDie("pick")
    _add_combined_capacity(routing, solver, deliv_dim, pick_dim, cap, n_nodes, manager)

    extra_dims: dict[str, tuple[pywrapcp.RoutingDimension, pywrapcp.RoutingDimension]] = {}
    if settings.truck_weight_lb_cap:
        wcap = round(settings.truck_weight_lb_cap)
        w_deliv = routing.RegisterUnaryTransitVector(
            [0] + [-round(s.delivery_weight) for s in stops]
        )
        w_pick = routing.RegisterUnaryTransitVector([0] + [round(s.pickup_weight) for s in stops])
        routing.AddDimension(w_deliv, 0, wcap, False, "w_deliv")
        routing.AddDimension(w_pick, 0, wcap, True, "w_pick")
        wd, wp = routing.GetDimensionOrDie("w_deliv"), routing.GetDimensionOrDie("w_pick")
        _add_combined_capacity(routing, solver, wd, wp, wcap, n_nodes, manager)
        extra_dims["weight"] = (wd, wp)
    if settings.truck_pieces_cap:
        pcap = int(settings.truck_pieces_cap)
        p_deliv = routing.RegisterUnaryTransitVector([0] + [-s.pieces_in for s in stops])
        p_pick = routing.RegisterUnaryTransitVector([0] + [s.pieces_out for s in stops])
        routing.AddDimension(p_deliv, 0, pcap, False, "p_deliv")
        routing.AddDimension(p_pick, 0, pcap, True, "p_pick")
        pd, pp = routing.GetDimensionOrDie("p_deliv"), routing.GetDimensionOrDie("p_pick")
        _add_combined_capacity(routing, solver, pd, pp, pcap, n_nodes, manager)
        extra_dims["pieces"] = (pd, pp)

    # ---- tiers / disjunctions --------------------------------------------------------
    penalties: list[int] = []
    for i, stop in enumerate(stops, start=1):
        penalty = settings.penalty_for_tier(stop.tier)
        penalties.append(penalty)
        routing.AddDisjunction([manager.NodeToIndex(i)], penalty)

    # ---- presets: fixed cost per truck (slot 1 only) and global span -----------------
    fixed = settings.effective_truck_fixed_cost
    for s in slots:
        routing.SetFixedCostOfVehicle(fixed if s.slot == 1 else 0, s.vehicle)
    span_coef = settings.effective_span_cost_coefficient
    if span_coef > 0:
        time_dim.SetGlobalSpanCostCoefficient(span_coef)

    # ---- lunch -----------------------------------------------------------------------
    node_visit_transits = [0] * routing.Size()
    for index in range(routing.Size()):
        node = manager.IndexToNode(index)
        node_visit_transits[index] = service_s[node] if node > 0 else 0
    breaks = _add_lunch(routing, solver, time_dim, slots, settings, node_visit_transits, k_slots)

    # ---- search parameters -----------------------------------------------------------
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = getattr(
        routing_enums_pb2.FirstSolutionStrategy, settings.first_solution_strategy
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = int(settings.solve_time_seconds)
    params.log_search = False

    return BuiltModel(
        manager=manager,
        routing=routing,
        time_dim=time_dim,
        deliv_dim=deliv_dim,
        pick_dim=pick_dim,
        slots=slots,
        breaks=breaks,
        node_service_s=service_s,
        penalties=penalties,
        search_parameters=params,
        lunch_mode=settings.lunch_mode,
        extra_dims=extra_dims,
    )


def _add_combined_capacity(
    routing: pywrapcp.RoutingModel,
    solver: pywrapcp.Solver,
    deliv_dim: pywrapcp.RoutingDimension,
    pick_dim: pywrapcp.RoutingDimension,
    cap: int,
    n_nodes: int,
    manager: pywrapcp.RoutingIndexManager,
) -> None:
    """deliv + pick <= cap on every stop arrival and on every vehicle end; deliv end == 0."""
    for node in range(1, n_nodes):
        index = manager.NodeToIndex(node)
        solver.Add(deliv_dim.CumulVar(index) + pick_dim.CumulVar(index) <= cap)
    for v in range(routing.vehicles()):
        end = routing.End(v)
        deliv_dim.CumulVar(end).SetRange(0, 0)
        solver.Add(deliv_dim.CumulVar(end) + pick_dim.CumulVar(end) <= cap)


def _lunch_budget(settings: RunSettings, k_slots: int) -> tuple[int, int]:
    """(seconds added to slot 1's return arc, seconds added to slot >= 2 start arcs).

    Smart mode budgets the lunch as time on load 1's return arc.  When the reload is long
    enough to host lunch, slot >= 2 start arcs carry ``reload - lunch`` so a truck with a
    second load pays exactly the reload (lunch happens during it) while a single-load
    truck pays the lunch once.  Simple mode uses explicit break intervals instead.
    """
    if settings.lunch_mode != "smart" or settings.lunch_seconds <= 0:
        return 0, settings.reload_seconds
    lunch = settings.lunch_seconds
    if lunch_via_reload(settings) and k_slots >= 2:
        return lunch, settings.reload_seconds - lunch
    return lunch, settings.reload_seconds


def _add_lunch(
    routing: pywrapcp.RoutingModel,
    solver: pywrapcp.Solver,
    time_dim: pywrapcp.RoutingDimension,
    slots: list[VehicleSlot],
    settings: RunSettings,
    node_visit_transits: list[int],
    k_slots: int,
) -> dict[int, Any]:
    """Lunch per brief 7.5 and ASSUMPTIONS.md.

    * ``smart`` (default): no interval variables - the lunch is a time budget on load 1
      (see :func:`_lunch_budget`); the timeline engine places the gap deterministically
      (during the reload for multi-load trucks, otherwise on the first arc that departs
      inside the lunch window) and the validator checks it.
    * ``simple``: a mandatory ``FixedDurationIntervalVar`` on every truck's slot-1
      vehicle, confined to that vehicle's route span, placed by the solver inside the
      lunch window.  Over-conservative for multi-load trucks but robust.
    """
    lunch_s = settings.lunch_seconds
    if lunch_s <= 0 or settings.lunch_mode == "smart":
        return {}
    earliest, latest = settings.lunch_window_rel()
    horizon = settings.on_duty_seconds
    breaks: dict[int, Any] = {}
    for s in slots:
        if s.slot != 1:
            continue
        b = solver.FixedDurationIntervalVar(earliest, latest, lunch_s, False, f"lunch_v{s.vehicle}")
        time_dim.SetBreakIntervalsOfVehicle([b], s.vehicle, node_visit_transits)
        start = routing.Start(s.vehicle)
        end = routing.End(s.vehicle)
        solver.Add(b.SafeStartExpr(horizon) >= time_dim.CumulVar(start))
        solver.Add(b.SafeEndExpr(0) <= time_dim.CumulVar(end))
        breaks[s.vehicle] = b
    return breaks
