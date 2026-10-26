"""KPI computation for a Plan (brief 8.1)."""

from __future__ import annotations

from routeopt.config import RunSettings
from routeopt.domain import DayTag, KPIs, Plan, Stop


def compute_kpis(
    plan: Plan, stops: list[Stop], settings: RunSettings, trucks_selected: int
) -> KPIs:
    stops_by_id = {s.stop_id: s for s in stops}
    used = plan.used_trucks
    loads = [load for t in used for load in t.loads if load.stops]
    served_ids = plan.served_stop_ids
    served = [stops_by_id[sid] for sid in served_ids if sid in stops_by_id]
    d_total = [s for s in stops if s.day_tag == DayTag.D]
    d_served = [s for s in served if s.day_tag == DayTag.D]
    pf_included: dict[str, int] = {}
    pf_available: dict[str, int] = {}
    for tag in (DayTag.D1, DayTag.D2):
        pf_available[tag.value] = sum(1 for s in stops if s.day_tag == tag)
        pf_included[tag.value] = sum(1 for s in served if s.day_tag == tag)
    unassigned_by_tier: dict[str, int] = {}
    for u in plan.unassigned:
        key = f"tier {u.tier}"
        unassigned_by_tier[key] = unassigned_by_tier.get(key, 0) + 1
    unassigned_shipments = sum(
        len(stops_by_id[u.stop_id].shipments) for u in plan.unassigned if u.stop_id in stops_by_id
    )
    cap = settings.truck_volume_cuft
    load_util = {
        f"{t.number}-L{load.seq}": round(100 * load.load_out_volume / cap, 1)
        for t in used
        for load in t.loads
        if load.stops
    }
    on_duty = [t.on_duty_minutes for t in used]
    available_minutes = trucks_selected * (settings.on_duty_hours * 60 - settings.lunch_minutes)
    return KPIs(
        trucks_used=len(used),
        trucks_selected=trucks_selected,
        loads=len(loads),
        stops_served=len(served_ids),
        shipments_served=sum(len(s.shipments) for s in served),
        d_stops_total=len(d_total),
        d_stops_served=len(d_served),
        d_stops_served_pct=round(100 * len(d_served) / len(d_total), 1) if d_total else 100.0,
        pull_forward_included=pf_included,
        pull_forward_available=pf_available,
        unassigned_by_tier=dict(sorted(unassigned_by_tier.items())),
        unassigned_stops=len(plan.unassigned),
        unassigned_shipments=unassigned_shipments,
        total_drive_minutes=round(sum(load.drive_minutes for load in loads), 1),
        total_miles=round(sum(load.distance_miles for load in loads), 1),
        avg_on_duty_minutes=round(sum(on_duty) / len(on_duty), 1) if on_duty else 0.0,
        max_on_duty_minutes=round(max(on_duty), 1) if on_duty else 0.0,
        fleet_volume_utilization_pct=round(sum(load_util.values()) / len(load_util), 1)
        if load_util
        else 0.0,
        load_utilization_pct=load_util,
        avg_stops_per_load=round(len(served_ids) / len(loads), 1) if loads else 0.0,
        demand_delivery_volume=round(sum(s.delivery_volume for s in d_total), 1),
        available_volume=round(trucks_selected * cap, 1),
        demand_service_minutes=round(sum(s.service_total_minutes for s in d_total), 1),
        available_minutes=round(available_minutes, 1),
    )
