"""Re-validation, versioning helpers and the export gate for edited plans (brief 10)."""

from __future__ import annotations

from dataclasses import dataclass

from routeopt.domain import Plan, Violation, hub_now
from routeopt.edit.operations import REMOVED_BY_PLANNER, EditContext, EditError
from routeopt.solver.extract import rebuild_plan_trucks
from routeopt.solver.plan_validator import validate_plan


def revalidate(ctx: EditContext, plan: Plan | None = None) -> Plan:
    """Rebuild times/volumes/KPIs from the plan's structure and run the validator.

    Uses the cached travel matrix only - no solver call - so it is fast enough to run
    after every click.  Unassigned stops keep their reasons; stops newly taken off a route
    get "Removed from a route by the planner".
    """
    source = plan if plan is not None else ctx.plan
    known_reasons = {u.stop_id: u for u in source.unassigned}
    rebuilt = rebuild_plan_trucks(source, ctx.stops, ctx.settings, ctx.matrix)
    fixed = []
    for u in rebuilt.unassigned:
        prev = known_reasons.get(u.stop_id)
        if prev is not None and prev.reason:
            fixed.append(u.model_copy(update={"reason": prev.reason, "detail": prev.detail}))
        else:
            fixed.append(u.model_copy(update={"reason": u.reason or REMOVED_BY_PLANNER}))
    rebuilt.unassigned = fixed
    rebuilt.violations = validate_plan(rebuilt, ctx.stops, ctx.settings, ctx.matrix)
    return rebuilt


def new_version(plan: Plan, version: int) -> Plan:
    """Copy of the plan stamped as a new planner version (override reason cleared)."""
    return plan.model_copy(
        update={
            "plan_version": version,
            "created_at": hub_now(),
            "export_override_reason": None,
        },
        deep=True,
    )


@dataclass
class ExportGate:
    allowed: bool
    hard_violations: list[Violation]
    message: str


def can_export(plan: Plan) -> ExportGate:
    """Exports are blocked by hard violations unless the planner ticked 'Export anyway'."""
    hard = plan.hard_violations
    if not hard:
        return ExportGate(True, [], "No hard violations.")
    if plan.export_override_reason and plan.export_override_reason.strip():
        return ExportGate(
            True,
            hard,
            f"Exporting anyway ({len(hard)} hard violations): {plan.export_override_reason}",
        )
    preview = "; ".join(str(v) for v in hard[:3])
    more = f" (+{len(hard) - 3} more)" if len(hard) > 3 else ""
    return ExportGate(
        False,
        hard,
        f"{len(hard)} hard violation(s): {preview}{more}. Tick 'Export anyway' and give a "
        "reason to export this version.",
    )


def set_export_override(plan: Plan, reason: str) -> Plan:
    """Return a copy carrying the planner's override reason (logged and printed on exports)."""
    if not reason or not reason.strip():
        raise EditError("Please give a reason for exporting a plan with hard violations.")
    return plan.model_copy(update={"export_override_reason": reason.strip()}, deep=True)


def violations_by_stop(plan: Plan) -> dict[str, list[Violation]]:
    """Group violations for inline highlighting: by stop id, and 'truck:load' for load-level."""
    out: dict[str, list[Violation]] = {}
    for v in plan.violations:
        if v.stop_id:
            out.setdefault(v.stop_id, []).append(v)
        elif v.truck and v.load:
            out.setdefault(f"{v.truck}:{v.load}", []).append(v)
        elif v.truck:
            out.setdefault(v.truck, []).append(v)
        else:
            out.setdefault("plan", []).append(v)
    return out


def _structure(plan: Plan) -> dict[str, tuple[str, int, int]]:
    return {
        v.stop_id: (t.number, load.seq, v.seq)
        for t in plan.trucks
        for load in t.loads
        for v in load.stops
    }


def diff_plans(a: Plan, b: Plan) -> list[str]:
    """Short human lines describing structural differences from plan a to plan b.

    Re-sequencing is reported only when the relative order of stops that stay on the same
    load changes (renumbering caused by a removal is not a re-sequence).
    """
    sa, sb = _structure(a), _structure(b)
    lines: list[str] = []
    for stop_id in sorted(set(sa) | set(sb)):
        before, after = sa.get(stop_id), sb.get(stop_id)
        if before is None and after is not None:
            lines.append(f"{stop_id}: assigned to {after[0]} load {after[1]} #{after[2]}")
        elif after is None and before is not None:
            lines.append(f"{stop_id}: removed from {before[0]} load {before[1]} #{before[2]}")
        elif before is not None and after is not None and before[:2] != after[:2]:
            lines.append(
                f"{stop_id}: moved from {before[0]} load {before[1]} #{before[2]} to "
                f"{after[0]} load {after[1]} #{after[2]}"
            )
    # relative order among stops that stayed on the same load
    loads = {(t, load) for (t, load, _s) in sb.values()}
    for truck, load_seq in sorted(loads):
        common = [
            sid
            for sid in sa
            if sid in sb and sa[sid][:2] == (truck, load_seq) and sb[sid][:2] == (truck, load_seq)
        ]
        order_a = sorted(common, key=lambda sid: sa[sid][2])
        order_b = sorted(common, key=lambda sid: sb[sid][2])
        if order_a != order_b:
            lines.append(
                f"{truck} load {load_seq}: re-sequenced ({' > '.join(order_a)} -> {' > '.join(order_b)})"
            )
    return lines
