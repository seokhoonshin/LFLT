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
from routeopt.solver.model_builder import VehicleSlot, vehicle_layout
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
    slots: list[VehicleSlot] | None = None,
) -> list[list[int]]:
    """Return one node list per virtual vehicle (see :func:`vehicle_layout`).

    Each load takes stops of one site only (the first site in sweep order that still has
    a stop that fits) and runs on that site's vehicle, a pallet-trip vehicle when one of
    its stops needs a pallet.
    """
    k_slots = settings.max_loads_per_truck
    slots = slots if slots is not None else vehicle_layout(stops, settings, len(trucks))
    vehicle = {(s.truck_index, s.slot, s.site, s.pallet): s.vehicle for s in slots}
    has_pallet_trips = any(s.pallet for s in slots)
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    remaining = sweep_order(stops)
    routes: list[list[int]] = [[] for _ in slots]
    for t_idx, truck in enumerate(trucks):
        if not remaining:
            break
        loads: list[list[str]] = []
        for _slot in range(k_slots):
            current: list[str] = []
            loads.append(current)
            for site in dict.fromkeys(s.site for s in remaining):
                leftovers: list[Stop] = []
                for stop in remaining:
                    if stop.site != site:
                        leftovers.append(stop)
                        continue
                    current.append(stop.stop_id)
                    if not _feasible(truck, loads, stops_by_id, matrix, node_of, settings):
                        current.pop()
                        leftovers.append(stop)
                remaining = leftovers
                if current:
                    break
            if not current:
                loads.pop()
                break
            if not remaining:
                break
        for k, load in enumerate(loads, start=1):
            pallet = has_pallet_trips and any(stops_by_id[sid].pallet for sid in load)
            v = vehicle[(t_idx, k, stops_by_id[load[0]].site, pallet)]
            routes[v] = [node_of[sid] for sid in load]
    return routes
