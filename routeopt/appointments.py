"""Appointment tracking: the trucks the planner picks on Results, one row per shipment.

Picking a truck snapshots its loads (stop order, ETA, volume, contact) so the list stays
as it was requested even if the plan is edited later; the planner then sets each
shipment's status (requested / pending / confirmed / declined) on the Appointments page.
Rows are kept as history, so the confirmed and declined share can be tracked over time.
"""

from __future__ import annotations

from typing import Any

from routeopt.domain import Plan, Stop, clock

STATUSES = ["Not requested", "Requested", "Pending", "Confirmed", "Declined"]
# a stop's outcome: confirmed when every shipment is, declined (the customer can't take
# it that day; dropped from the route) when any is, else still open
OUTCOMES = ["Confirmed", "Declined", "Open"]


def appointment_rows(plan: Plan, stops: list[Stop], truck_number: str) -> list[dict[str, Any]]:
    """One row per shipment on the truck's loads, in load / stop order."""
    by_id = {s.stop_id: s for s in stops}
    truck = plan.truck(truck_number)
    rows: list[dict[str, Any]] = []
    for load in truck.loads:
        for visit in load.stops:
            stop = by_id.get(visit.stop_id)
            if stop is None:
                continue
            for sh in stop.shipments:
                rows.append(
                    {
                        "run_id": plan.run_id,
                        "version": plan.plan_version,
                        "truck": truck.number,
                        "load_seq": load.seq,
                        "stop_seq": visit.seq,
                        "stop_id": stop.stop_id,
                        "shipment_id": sh.shipment_id,
                        "customer": sh.customer_name,
                        "address": f"{stop.address}, {stop.city} {stop.zip}",
                        "bill_to_code": sh.bill_to_code,
                        "delivery_date": plan.plan_date,
                        "eta": clock(visit.arrive_s),
                        "window": f"{clock(visit.eta_window_start_s)} – {clock(visit.eta_window_end_s)}",
                        "volume_cuft": round(sh.volume_cuft, 2),
                        "miles_from_prev": visit.dist_mi_from_prev,
                        "stop_transit_min": visit.transit_min_from_prev,
                        "poc": stop.contact,
                        "dc": load.site,
                    }
                )
    return rows


def stop_outcome(statuses: list[str]) -> str:
    if statuses and all(s == "Confirmed" for s in statuses):
        return "Confirmed"
    if any(s == "Declined" for s in statuses):
        return "Declined"
    return "Open"


def stop_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per picked stop: delivery day, truck, volume and its outcome."""
    stops: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        key = (r["run_id"], r["truck"], r["load_seq"], r["stop_seq"])
        st = stops.setdefault(
            key,
            {
                "delivery_date": r["delivery_date"],
                "run_id": r["run_id"],
                "truck": r["truck"],
                "load_seq": r["load_seq"],
                "stop_seq": r["stop_seq"],
                "customer": r["customer"],
                "volume_cuft": 0.0,
                "statuses": [],
            },
        )
        st["volume_cuft"] += r["volume_cuft"] or 0.0
        st["statuses"].append(r["status"])
    out = []
    for st in stops.values():
        st["outcome"] = stop_outcome(st.pop("statuses"))
        out.append(st)
    return out


def outcome_summary(stops: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per outcome: stops, % of stops, cu ft, % of cu ft."""
    n = len(stops) or 1
    vol = sum(s["volume_cuft"] for s in stops) or 1.0
    out: dict[str, dict[str, float]] = {}
    for o in OUTCOMES:
        sel = [s for s in stops if s["outcome"] == o]
        v = sum(s["volume_cuft"] for s in sel)
        out[o] = {
            "stops": len(sel),
            "stops_pct": round(100 * len(sel) / n, 1),
            "volume": round(v, 1),
            "volume_pct": round(100 * v / vol, 1),
        }
    return out


def stop_status(statuses: list[str]) -> str:
    """One status for a stop: the shared one, else the least advanced (so a stop shows
    Confirmed only when every shipment is; any Declined wins)."""
    if "Declined" in statuses:
        return "Declined"
    if len(set(statuses)) == 1:
        return statuses[0]
    return min(statuses, key=STATUSES.index)


def group_by_stop(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shipment rows -> one row per stop (same truck, load and stop), keeping the row ids
    so a status set on the stop can be written to each of its shipments."""
    stops: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        key = (r["run_id"], r["version"], r["truck"], r["load_seq"], r["stop_seq"])
        st = stops.get(key)
        if st is None:
            st = stops[key] = {
                **{k: r[k] for k in r if k not in ("id", "shipment_id", "volume_cuft")},
                "ids": [],
                "shipment_ids": [],
                "volume_cuft": 0.0,
                "statuses": [],
                "notes": [],
                "bill_tos": [],
            }
        st["ids"].append(r["id"])
        st["shipment_ids"].append(r["shipment_id"])
        st["volume_cuft"] += r["volume_cuft"] or 0.0
        st["statuses"].append(r["status"])
        if r.get("note") and r["note"] not in st["notes"]:
            st["notes"].append(r["note"])
        if r.get("bill_to_code") and r["bill_to_code"] not in st["bill_tos"]:
            st["bill_tos"].append(r["bill_to_code"])
    out = []
    for st in stops.values():
        st["shipments"] = len(st["ids"])
        st["volume_cuft"] = round(st["volume_cuft"], 1)
        st["status"] = stop_status(st.pop("statuses"))
        st["note"] = " | ".join(st.pop("notes"))
        st["bill_to_code"] = ", ".join(st.pop("bill_tos")) or None
        st["shipment_ids"] = ", ".join(st["shipment_ids"])
        out.append(st)
    return out
