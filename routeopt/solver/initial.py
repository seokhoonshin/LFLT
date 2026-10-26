"""Slot-aware greedy construction of an initial solution for the routing search.

OR-Tools' built-in first-solution heuristics are not aware of the load-slot symmetry
(slot 2 of a truck may only be used when slot 1 is), so their solutions are frequently
rejected and the search starts from "everything dropped".  This sweep-and-append
heuristic builds a feasible, symmetry-respecting starting point using the same timeline
engine and rule checks as the validator, so whatever it produces is accepted by the model.
"""

from __future__ import annotations

import math

from routeopt.config import RunSettings
from routeopt.domain import HUB_LAT, HUB_LON, Stop, TruckConfig
from routeopt.solver.plan_validator import check_truck
from routeopt.solver.timeline import RouteSkeleton, build_truck, node_index_map
from routeopt.travel.base import Matrix


def _angle(stop: Stop) -> float:
    return math.atan2(stop.latitude - HUB_LAT, stop.longitude - HUB_LON)


def sweep_order(stops: list[Stop]) -> list[Stop]:
    """D stops first (by tier), each group swept by angle around the hub."""
    out: list[Stop] = []
    for tier in range(1, 6):
        group = [s for s in stops if s.tier == tier]
        out.extend(sorted(group, key=_angle))
    return out


def _feasible(
    truck: TruckConfig,
    loads: list[list[str]],
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> bool:
    skel = RouteSkeleton(
        truck_number=truck.truck_number, driver_name=truck.driver_name, loads=loads
    )
    rebuilt = build_truck(skel, stops_by_id, matrix, node_of, settings)
    return not any(p.severity == "error" for p in check_truck(rebuilt, stops_by_id, settings))


def greedy_routes(
    stops: list[Stop],
    matrix: Matrix,
    settings: RunSettings,
    trucks: list[TruckConfig],
) -> list[list[int]]:
    """Return one node list per virtual vehicle (truck-major, slot-minor)."""
    k_slots = settings.max_loads_per_truck
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    remaining = sweep_order(stops)
    routes: list[list[int]] = [[] for _ in range(len(trucks) * k_slots)]
    truck_loads: dict[int, list[list[str]]] = {}
    for t_idx, truck in enumerate(trucks):
        if not remaining:
            break
        loads: list[list[str]] = []
        truck_loads[t_idx] = loads
        for _slot in range(k_slots):
            current: list[str] = []
            loads.append(current)
            leftovers: list[Stop] = []
            for stop in remaining:
                current.append(stop.stop_id)
                if not _feasible(truck, loads, stops_by_id, matrix, node_of, settings):
                    current.pop()
                    leftovers.append(stop)
            remaining = leftovers
            if not current:
                loads.pop()
                break
            if not remaining:
                break
        for k, load in enumerate(loads):
            routes[t_idx * k_slots + k] = [node_of[sid] for sid in load]
    return routes
