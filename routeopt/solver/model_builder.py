"""Build the OR-Tools routing model (brief section 7).

Nodes: 0..N_DEPOTS-1 = loading sites, then the stops.  Vehicles: one *virtual vehicle* per
(truck, load slot, site, trip kind); at most one of each (truck, slot) group runs.
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
from routeopt.domain import DEFAULT_SITE, N_DEPOTS, SITE_NODE, Stop, TruckConfig
from routeopt.solver.timeline import lunch_via_reload
from routeopt.travel.base import Matrix

METERS_PER_MILE = 1609.344


@dataclass
class SolverInputs:
    stops: list[Stop]  # node i+1
    matrix: Matrix  # over [hub] + stops
    settings: RunSettings
    trucks: list[TruckConfig]  # selected trucks, in order
    # stop_id -> truck number: the stop may only go on that truck and is kept whenever it
    # fits (used to re-plan around declined appointments without moving accepted ones)
    pinned: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.matrix.n != len(self.stops) + N_DEPOTS:
            raise ValueError("matrix size must equal number of stops + the depot nodes")


@dataclass
class VehicleSlot:
    vehicle: int
    truck_index: int
    slot: int  # 1..K
    site: str = DEFAULT_SITE
    pallet: bool = False  # pallet trip: uses the pallet volume limit


def vehicle_layout(stops: list[Stop], settings: RunSettings, n_trucks: int) -> list[VehicleSlot]:
    """One virtual vehicle per (truck, load slot, loading site, trip kind), truck-major.

    Only the sites that have stops get vehicles; pallet-trip vehicles exist only when a
    pallet limit is set and some stop needs a pallet.  At most one vehicle per (truck,
    slot) is active (see :func:`build_model`).
    """
    sites = sorted({s.site for s in stops} or {DEFAULT_SITE}, key=lambda c: SITE_NODE[c])
    kinds = [False]
    if settings.pallet_cap_tenths is not None and any(s.pallet for s in stops):
        kinds.append(True)
    out: list[VehicleSlot] = []
    for t in range(n_trucks):
        for k in range(1, settings.max_loads_per_truck + 1):
            for site in sites:
                for pallet in kinds:
                    out.append(VehicleSlot(len(out), t, k, site, pallet))
    return out


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

    def group(self, truck_index: int, slot: int) -> list[VehicleSlot]:
        """The vehicles that can run load ``slot`` of a truck (one per site / trip kind)."""
        return [s for s in self.slots if s.truck_index == truck_index and s.slot == slot]


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
    n_dep = N_DEPOTS
    n_nodes = len(stops) + n_dep
    k_slots = settings.max_loads_per_truck
    n_trucks = len(inputs.trucks)
    slots = vehicle_layout(stops, settings, n_trucks)
    n_vehicles = len(slots)
    # Solver time 0 = settings.model_origin_s.  With a floating start (CA) that is the
    # earliest possible driver start; each truck's start is a variable, its day is capped
    # at on_duty from that start, and a first stop reached after the appointment costs
    # late_first_stop_cost per second (so near first stops are preferred).
    offset = settings.model_offset_s
    first_rel = settings.earliest_delivery_rel_s
    floating = settings.floating
    latest_start = offset + max(0, first_rel - settings.first_load_seconds) if floating else 0
    horizon = latest_start + settings.on_duty_seconds
    cap = settings.volume_cap_tenths
    pallet_cap = settings.pallet_cap_tenths if settings.pallet_cap_tenths is not None else cap
    vehicle_caps = [pallet_cap if s.pallet else cap for s in slots]
    depots = [SITE_NODE[s.site] for s in slots]

    manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, depots, depots)
    routing = pywrapcp.RoutingModel(manager)
    solver = routing.solver()
    # a site with no vehicles is still a node of the matrix: never visit it
    for node in set(range(n_dep)) - set(depots):
        index = manager.NodeToIndex(node)
        routing.AddDisjunction([index], 0)
        routing.ActiveVar(index).SetValue(0)

    # ---- transit matrices ------------------------------------------------------------
    # transit(i, j) = travel + service at the from-node.  Slot 1's start arcs carry the
    # first-load loading time and its return arcs the lunch budget ("smart" mode); slot
    # >= 2 start arcs carry the reload (minus the lunch already budgeted when the reload
    # is long enough to host it) - see _lunch_budget().  Depot -> depot (an empty
    # vehicle) stays 0.
    service_s = [0] * n_dep + [s.service_total_minutes * 60 for s in stops]
    travel = inputs.matrix.durations_s
    lunch_on_return, reload_extra = _lunch_budget(settings, k_slots)
    time_slot1 = [[travel[i][j] + service_s[i] for j in range(n_nodes)] for i in range(n_nodes)]
    time_slotk = [row[:] for row in time_slot1]
    last_dep = [row[:] for row in time_slot1]
    for i in range(n_dep, n_nodes):
        for d in range(n_dep):
            time_slot1[i][d] += lunch_on_return  # stop -> site on slot 1: lunch budget
            last_dep[i][d] = service_s[i]  # ends at the departure from the last stop
    for d in range(n_dep):
        for j in range(n_dep, n_nodes):
            time_slot1[d][j] += settings.first_load_seconds
            time_slotk[d][j] += reload_extra
    last_dep_k = [row[:] for row in last_dep]
    for d in range(n_dep):
        for j in range(n_dep, n_nodes):
            last_dep[d][j] = time_slot1[d][j]
            last_dep_k[d][j] = time_slotk[d][j]

    # the cost of driving (time dimensions keep the real ``travel``).  Goal "cluster":
    # stop -> stop driving costs (1 + cluster_weight) x, so a truck takes stops that are
    # near each other instead of zig-zagging between areas.  Depot legs are unchanged.
    cost = travel
    if settings.goal == "cluster" and settings.cluster_weight > 0:
        factor = 1 + settings.cluster_weight
        cost = [row[:] for row in travel]
        for i in range(n_dep, n_nodes):
            for j in range(n_dep, n_nodes):
                if i != j:
                    cost[i][j] = round(travel[i][j] * factor)
    travel_cb = routing.RegisterTransitMatrix(cost)
    routing.SetArcCostEvaluatorOfAllVehicles(travel_cb)
    if settings.floating and settings.late_first_stop_cost:
        # load 1 leaves at start + first loading and the start never precedes time 0 (the
        # DC opening): a first stop farther than appointment - loading - start-at-0 is
        # reached late.  Charge that lateness on the DC -> first-stop arc of load 1.
        slack_to_appt = settings.model_offset_s + settings.earliest_delivery_rel_s
        slack_to_appt -= settings.first_load_seconds
        first_cost = [row[:] for row in cost]
        for d in range(n_dep):
            for j in range(n_dep, n_nodes):
                late = max(0, travel[d][j] - slack_to_appt)
                first_cost[d][j] += settings.late_first_stop_cost * late
        first_cb = routing.RegisterTransitMatrix(first_cost)
        for s in slots:
            if s.slot == 1:
                routing.SetArcCostEvaluatorOfVehicle(first_cb, s.vehicle)
    cb_slot1 = routing.RegisterTransitMatrix(time_slot1)
    cb_slotk = routing.RegisterTransitMatrix(time_slotk)
    time_cbs = [cb_slot1 if s.slot == 1 else cb_slotk for s in slots]

    # ---- time dimension --------------------------------------------------------------
    routing.AddDimensionWithVehicleTransits(time_cbs, horizon, horizon, False, "time")
    time_dim = routing.GetDimensionOrDie("time")
    # departure from a load's last stop (for the gap between turns): a twin of the time
    # dimension whose return arcs carry only the last stop's service
    dep_dim: pywrapcp.RoutingDimension | None = None
    if settings.turn_gap_seconds and k_slots >= 2:
        cb_ld1 = routing.RegisterTransitMatrix(last_dep)
        cb_ldk = routing.RegisterTransitMatrix(last_dep_k)
        routing.AddDimensionWithVehicleTransits(
            [cb_ld1 if s.slot == 1 else cb_ldk for s in slots], horizon, horizon, False, "last_dep"
        )
        dep_dim = routing.GetDimensionOrDie("last_dep")
        for node in range(n_dep, n_nodes):
            index = manager.NodeToIndex(node)
            solver.Add(dep_dim.CumulVar(index) == time_dim.CumulVar(index))
    if first_rel:
        for node in range(n_dep, n_nodes):
            time_dim.CumulVar(manager.NodeToIndex(node)).SetMin(offset + first_rel)

    def active(v: int) -> Any:
        return routing.ActiveVehicleVar(v)

    for s in slots:
        start = routing.Start(s.vehicle)
        end = routing.End(s.vehicle)
        if dep_dim is not None:
            solver.Add(dep_dim.CumulVar(start) == time_dim.CumulVar(start))
        if s.slot == 1:
            time_dim.CumulVar(start).SetRange(0, latest_start)
        if not (s.slot == 1 and floating):
            routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(start))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(end))
        if disabled_trucks and s.truck_index in disabled_trucks:
            solver.Add(active(s.vehicle) == 0)
    if dep_dim is not None or floating:  # report the earliest arrivals, as the timeline does
        for node in range(n_dep, n_nodes):
            routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(manager.NodeToIndex(node)))
    if floating:  # then start each driver as late as those arrivals allow (no idle wait)
        for s in slots:
            if s.slot == 1:
                routing.AddVariableMaximizedByFinalizer(time_dim.CumulVar(routing.Start(s.vehicle)))
    if floating:
        for t in range(n_trucks):
            firsts = [s.vehicle for s in slots if s.truck_index == t and s.slot == 1]
            if not firsts:
                continue
            day_start = time_dim.CumulVar(routing.Start(firsts[0]))
            for v in firsts[1:]:  # one driver start per truck, whichever site loads first
                solver.Add(time_dim.CumulVar(routing.Start(v)) == day_start)
            for s in slots:  # every turn ends within on-duty hours of the driver start
                if s.truck_index == t:
                    solver.Add(
                        time_dim.CumulVar(routing.End(s.vehicle))
                        <= day_start + settings.on_duty_seconds
                    )
        # The driver starts exactly so the first stop is reached at the first appointment:
        # start = appointment - first loading - drive to that stop (never before time 0,
        # the earliest start).  No waiting on the first arc, and the start is capped by the
        # value for whichever stop comes first; the minimum delivery time does the rest.
        appointment = offset + first_rel
        n_index = routing.Size() + n_vehicles
        for s in slots:
            if s.slot != 1:
                continue
            start_index = routing.Start(s.vehicle)
            time_dim.SlackVar(start_index).SetValue(0)
            bounds = [latest_start] * n_index
            for index in range(routing.Size()):
                node = manager.IndexToNode(index)
                if node >= n_dep:
                    bounds[index] = max(
                        0,
                        appointment - settings.first_load_seconds - travel[depots[s.vehicle]][node],
                    )
            solver.Add(
                time_dim.CumulVar(start_index)
                <= solver.Element(bounds, routing.NextVar(start_index))
            )
    for t in range(n_trucks):
        for k in range(1, k_slots + 1):
            group = [s.vehicle for s in slots if s.truck_index == t and s.slot == k]
            solver.Add(solver.Sum([active(v) for v in group]) <= 1)  # one site / kind per load
            if k == 1:
                continue
            prev = [s for s in slots if s.truck_index == t and s.slot == k - 1]
            # symmetry breaking: load k only if load k-1 runs
            solver.Add(
                solver.Sum([active(v) for v in group])
                <= solver.Sum([active(u.vehicle) for u in prev])
            )
            for v in group:
                start_v = time_dim.CumulVar(routing.Start(v))
                site_v = depots[v]
                for u in prev:
                    # An unused vehicle ends where it starts, so chaining every pair keeps
                    # the bound tight; the extra terms only apply when load k really runs.
                    end_u = time_dim.CumulVar(routing.End(u.vehicle))
                    deadhead = travel[depots[u.vehicle]][site_v]
                    if deadhead:  # back at the DC, then DC-to-DC when loading elsewhere
                        solver.Add(start_v >= end_u + deadhead * active(v))
                    else:
                        solver.Add(start_v >= end_u)
                    if dep_dim is not None:
                        last_u = dep_dim.CumulVar(routing.End(u.vehicle))
                        solver.Add(
                            start_v + reload_extra >= last_u + settings.turn_gap_seconds * active(v)
                        )

    # ---- distance limits -------------------------------------------------------------
    dist = inputs.matrix.distances_m
    if settings.max_one_way_miles is not None:
        one_way = [
            [dist[i][j] if j >= n_dep else 0 for j in range(n_nodes)] for i in range(n_nodes)
        ]
        limit = int(settings.max_one_way_miles * METERS_PER_MILE) - 1
        routing.AddDimension(routing.RegisterTransitMatrix(one_way), 0, limit, True, "one_way")
    if settings.two_turn_max_miles is not None and k_slots >= 2:
        total_cap = int(settings.max_loads_per_truck * 2 * 250 * METERS_PER_MILE)
        routing.AddDimension(routing.RegisterTransitMatrix(dist), 0, total_cap, True, "miles")
        miles_dim = routing.GetDimensionOrDie("miles")
        limit = int(settings.two_turn_max_miles * METERS_PER_MILE) - 1
        for t in range(n_trucks):
            mine = [s.vehicle for s in slots if s.truck_index == t]
            later = [s.vehicle for s in slots if s.truck_index == t and s.slot >= 2]
            ends = solver.Sum([miles_dim.CumulVar(routing.End(v)) for v in mine])
            two_turn = solver.Sum([active(v) for v in later])
            solver.Add(ends <= limit + total_cap * k_slots * (1 - two_turn))

    # ---- which vehicles may serve a stop: its site; pallet stops only on pallet trips --
    for i, stop in enumerate(stops):
        index = manager.NodeToIndex(i + n_dep)
        banned = [
            s.vehicle
            for s in slots
            if s.site != stop.site
            or (stop.pallet and settings.pallet_cap_tenths is not None and not s.pallet)
        ]
        if banned:
            routing.VehicleVar(index).RemoveValues(banned)

    # ---- capacity: deliveries and pickups (VRPSPD) -----------------------------------
    deliv_tenths = [0] * n_dep + [_tenths(s.delivery_volume) for s in stops]
    pick_tenths = [0] * n_dep + [_tenths(s.pickup_volume) for s in stops]
    deliv_cb = routing.RegisterUnaryTransitVector([-x for x in deliv_tenths])
    pick_cb = routing.RegisterUnaryTransitVector(pick_tenths)
    routing.AddDimensionWithVehicleCapacity(deliv_cb, 0, vehicle_caps, False, "deliv")
    routing.AddDimensionWithVehicleCapacity(pick_cb, 0, vehicle_caps, True, "pick")
    deliv_dim = routing.GetDimensionOrDie("deliv")
    pick_dim = routing.GetDimensionOrDie("pick")
    _add_combined_capacity(routing, solver, deliv_dim, pick_dim, vehicle_caps, n_nodes, manager)

    extra_dims: dict[str, tuple[pywrapcp.RoutingDimension, pywrapcp.RoutingDimension]] = {}
    if settings.truck_weight_lb_cap:
        wcap = round(settings.truck_weight_lb_cap)
        w_deliv = routing.RegisterUnaryTransitVector(
            [0] * n_dep + [-round(s.delivery_weight) for s in stops]
        )
        w_pick = routing.RegisterUnaryTransitVector(
            [0] * n_dep + [round(s.pickup_weight) for s in stops]
        )
        routing.AddDimension(w_deliv, 0, wcap, False, "w_deliv")
        routing.AddDimension(w_pick, 0, wcap, True, "w_pick")
        wd, wp = routing.GetDimensionOrDie("w_deliv"), routing.GetDimensionOrDie("w_pick")
        _add_combined_capacity(routing, solver, wd, wp, [wcap] * n_vehicles, n_nodes, manager)
        extra_dims["weight"] = (wd, wp)
    if settings.truck_pieces_cap:
        pcap = int(settings.truck_pieces_cap)
        p_deliv = routing.RegisterUnaryTransitVector([0] * n_dep + [-s.pieces_in for s in stops])
        p_pick = routing.RegisterUnaryTransitVector([0] * n_dep + [s.pieces_out for s in stops])
        routing.AddDimension(p_deliv, 0, pcap, False, "p_deliv")
        routing.AddDimension(p_pick, 0, pcap, True, "p_pick")
        pd, pp = routing.GetDimensionOrDie("p_deliv"), routing.GetDimensionOrDie("p_pick")
        _add_combined_capacity(routing, solver, pd, pp, [pcap] * n_vehicles, n_nodes, manager)
        extra_dims["pieces"] = (pd, pp)

    # ---- tiers / disjunctions --------------------------------------------------------
    penalties: list[int] = []
    truck_no = {s.vehicle: inputs.trucks[s.truck_index].truck_number for s in slots}
    for i, stop in enumerate(stops, start=n_dep):
        penalty = settings.stop_penalty(stop.tier, stop.delivery_volume)
        owner = inputs.pinned.get(stop.stop_id)
        if owner is not None:
            # only on its truck, and dropping it costs far more than any other stop
            other = [v for v, number in truck_no.items() if number != owner]
            if other:
                routing.VehicleVar(manager.NodeToIndex(i)).RemoveValues(other)
            penalty *= 100
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
        node_visit_transits[index] = service_s[node]
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
    caps: list[int],
    n_nodes: int,
    manager: pywrapcp.RoutingIndexManager,
) -> None:
    """deliv + pick <= cap on every stop arrival (the largest vehicle cap; each dimension
    also holds its own vehicle cap) and <= the vehicle's cap on every end; deliv end == 0."""
    for node in range(N_DEPOTS, n_nodes):
        index = manager.NodeToIndex(node)
        solver.Add(deliv_dim.CumulVar(index) + pick_dim.CumulVar(index) <= max(caps))
    for v in range(routing.vehicles()):
        end = routing.End(v)
        deliv_dim.CumulVar(end).SetRange(0, 0)
        solver.Add(deliv_dim.CumulVar(end) + pick_dim.CumulVar(end) <= caps[v])


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
    offset = settings.model_offset_s  # solver time 0 is model_origin_s, not shift start
    earliest, latest = earliest + offset, latest + offset
    horizon = offset + 2 * settings.on_duty_seconds
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
