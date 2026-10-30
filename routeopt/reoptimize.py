"""Re-plan a run's picked trucks after customers declined appointments.

The declined stops are left out; every other stop already on a picked truck (not
requested, requested, pending or confirmed) is pinned to that truck; the run's other
candidates (unassigned stops, stops on trucks that were not picked) compete for the room
that is left.  The result is saved as a new version of the run and shown as the
Re-optimized set on the Appointments page; the original set stays as the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from routeopt.appointments import stop_outcome
from routeopt.db.repo import RunRecord
from routeopt.domain import N_DEPOTS, Plan, Stop
from routeopt.pipeline import optimize


@dataclass
class ReplanResult:
    plan: Plan
    stops: list[Stop]
    declined: list[str]  # stop ids left out
    pinned: dict[str, str]  # stop id -> truck
    added: list[str]  # stop ids new on the picked trucks


def declined_stop_ids(rows: list[dict[str, Any]]) -> set[str]:
    by_stop: dict[str, list[str]] = {}
    for r in rows:
        by_stop.setdefault(r["stop_id"], []).append(r["status"])
    return {sid for sid, statuses in by_stop.items() if stop_outcome(statuses) == "Declined"}


def replan(
    rec: RunRecord, rows: list[dict[str, Any]], exclude: set[str] | None = None
) -> ReplanResult:
    """``rows`` = the run's appointment rows to re-plan (every picked truck); ``exclude``
    = more declined stop ids (e.g. from the original set when re-planning a re-plan)."""
    if not rows:
        raise ValueError("No picked trucks for this run.")
    picked = {r["truck"] for r in rows}
    declined = declined_stop_ids(rows) | set(exclude or ())
    pinned = {r["stop_id"]: r["truck"] for r in rows if r["stop_id"] not in declined}
    keep = [i for i, s in enumerate(rec.stops) if s.stop_id not in declined]
    stops = [rec.stops[i] for i in keep]
    matrix = rec.matrix.submatrix(list(range(N_DEPOTS)) + [i + N_DEPOTS for i in keep])
    trucks = [t for t in rec.trucks if t.truck_number in picked]
    if not trucks:
        raise ValueError("The picked trucks are not part of this run.")
    settings = rec.settings.model_copy(update={"trucks_selected": [t.truck_number for t in trucks]})
    res = optimize(stops, matrix, settings, trucks, run_id=rec.run_id, pinned=pinned)
    plan = res.plan
    plan.plan_version = rec.latest_version + 1
    plan.warnings.insert(
        0,
        f"Re-planned after {len(declined)} declined stop(s): "
        f"{', '.join(sorted(declined)) or '-'} left out; accepted stops kept on their truck.",
    )
    served = {v.stop_id for t in plan.trucks for ld in t.loads for v in ld.stops}
    return ReplanResult(
        plan=plan,
        stops=res.all_stops,
        declined=sorted(declined),
        pinned=pinned,
        added=sorted(served - set(pinned)),
    )
