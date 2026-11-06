"""OBS add-on (CA LFLT): OBS stops are planned last and only to raise the stop count.

With ``obs_mode = "last"`` the optimizer first plans every stop except the OBS-only ones.
OBS stops are then inserted into the loads it built, cheapest extra on-duty time first,
but only into a load that already visits a non-OBS stop within ``obs_radius_miles``
(straight line), and only where every rule still holds (volume, on-duty, lunch, reload).
No planned stop is ever removed or moved to another load, and no new load is opened.
"""

from __future__ import annotations

from dataclasses import dataclass

from routeopt.config import RunSettings
from routeopt.domain import OrderType, Plan, PlanTruck, Stop, UnassignedStop
from routeopt.ingest.validator import haversine_miles
from routeopt.solver.kpis import compute_kpis
from routeopt.solver.timeline import node_index_map
from routeopt.solver.unassigned_reasons import _trial
from routeopt.travel.base import Matrix

REASON_OBS_EXCLUDED = "Not included: OBS stops are excluded from this run"
REASON_OBS_FAR = "OBS add-on: no planned stop nearby"
REASON_OBS_NO_ROOM = "OBS add-on: no room on a nearby load"


def is_obs_stop(stop: Stop) -> bool:
    """A stop is an OBS stop when every shipment on it is OBS (mixed stops plan normally)."""
    return bool(stop.order_types) and all(t == OrderType.OBS for t in stop.order_types)


def obs_unassigned(stop: Stop, reason: str, detail: str = "") -> UnassignedStop:
    return UnassignedStop(
        stop_id=stop.stop_id,
        tier=stop.tier,
        day_tag=stop.day_tag,
        volume=stop.delivery_volume,
        pickup_volume=stop.pickup_volume,
        service_minutes=stop.service_total_minutes,
        reason=reason,
        detail=detail,
    )


@dataclass
class _Candidate:
    extra_minutes: float
    stop: Stop
    truck: str
    loads: list[list[str]]
    rebuilt: PlanTruck


def add_obs_stops(
    plan: Plan,
    stops: list[Stop],
    obs_stops: list[Stop],
    settings: RunSettings,
    matrix: Matrix,
) -> Plan:
    """Insert OBS stops into ``plan`` (built without them).  ``stops``/``matrix`` cover every
    in-scope stop, OBS included, in node order."""
    stops_by_id = {s.stop_id: s for s in stops}
    node_of = node_index_map(stops)
    radius = settings.obs_radius_miles
    trucks = {t.number: t for t in plan.trucks}
    loads_of = {
        t.number: [[v.stop_id for v in load.stops] for load in t.loads if load.stops]
        for t in plan.trucks
    }
    # the planned (non-OBS) stops a load may anchor an OBS stop to; fixed so OBS stops
    # never chain off each other
    anchors = {num: [list(load) for load in loads] for num, loads in loads_of.items()}

    def near(stop: Stop, anchor_ids: list[str]) -> bool:
        return any(
            haversine_miles(
                stop.latitude,
                stop.longitude,
                stops_by_id[a].latitude,
                stops_by_id[a].longitude,
            )
            <= radius
            for a in anchor_ids
        )

    remaining = list(obs_stops)
    nearby: set[str] = set()
    added = 0
    while remaining:
        best: _Candidate | None = None
        for stop in remaining:
            for num, truck in trucks.items():
                base = loads_of[num]
                for li, load in enumerate(base):
                    if not near(stop, anchors[num][li]):
                        continue
                    nearby.add(stop.stop_id)
                    for pos in range(len(load) + 1):
                        loads = [list(x) for x in base]
                        loads[li].insert(pos, stop.stop_id)
                        ok, _failed, rebuilt = _trial(
                            truck, loads, stops_by_id, matrix, node_of, settings
                        )
                        if not ok:
                            continue
                        extra = rebuilt.on_duty_minutes - truck.on_duty_minutes
                        if best is None or extra < best.extra_minutes:
                            best = _Candidate(extra, stop, num, loads, rebuilt)
        if best is None:
            break
        trucks[best.truck] = best.rebuilt
        loads_of[best.truck] = best.loads
        remaining.remove(best.stop)
        added += 1

    unassigned = list(plan.unassigned)
    for stop in remaining:
        if stop.stop_id in nearby:
            unassigned.append(
                obs_unassigned(
                    stop,
                    REASON_OBS_NO_ROOM,
                    f"Within {radius:g} mi of a planned stop, but no load nearby has the time "
                    "or room left.",
                )
            )
        else:
            unassigned.append(
                obs_unassigned(
                    stop,
                    REASON_OBS_FAR,
                    f"No planned stop within {radius:g} mi; OBS stops are only added near "
                    "planned stops.",
                )
            )
    new_plan = plan.model_copy(
        update={
            "trucks": [trucks[t.number] for t in plan.trucks],
            "unassigned": unassigned,
            "warnings": [
                *plan.warnings,
                f"OBS add-on: {added} of {len(obs_stops)} OBS stop(s) added within "
                f"{radius:g} mi of a planned stop.",
            ],
        }
    )
    new_plan.kpis = compute_kpis(new_plan, stops, settings, len(plan.trucks))
    return new_plan
