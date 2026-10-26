"""Command-line interface: ``routeopt run sample.xlsx --preset A`` prints a plan.

Also: ``routeopt sample`` (generate a workbook), ``routeopt osrm-check`` (health check).
Exports and history are added by later milestones.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

from routeopt.config import AppConfig, RunSettings
from routeopt.domain import Plan, Stop, clock, hub_today
from routeopt.logging_utils import configure_logging


def _parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="routeopt", description="RouteOpt - same-day route optimizer")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="optimize a shipment workbook and print the plan")
    run.add_argument("workbook", type=Path)
    run.add_argument("--preset", choices=["A", "B", "Custom"], default="A")
    run.add_argument(
        "--profile",
        choices=["ca_lflt", "legacy"],
        default="ca_lflt",
        help="rule set: CA LFLT (default) or the legacy single-hub brief",
    )
    run.add_argument(
        "--plan-date", type=_parse_date, default=None, help="delivery day D (default: today + 2)"
    )
    run.add_argument("--solve-time", type=int, default=60, help="seconds, 1-60")
    run.add_argument("--pull-forward", default="", help="comma list of pull-forward days, e.g. 1,2")
    run.add_argument("--trucks", type=int, default=None, help="use only the first N trucks")
    run.add_argument("--lunch-mode", choices=["smart", "simple"], default="smart")
    run.add_argument("--provider", choices=["auto", "osrm", "haversine"], default="auto")
    run.add_argument("--max-loads", type=int, default=None, help="default: the profile's")
    run.add_argument(
        "--fixed-cost", type=int, default=None, help="Custom preset: fixed cost per truck"
    )
    run.add_argument(
        "--span-cost", type=int, default=None, help="Custom preset: span cost coefficient"
    )
    run.add_argument("--export", type=Path, default=None, help="folder for Excel + PDF exports")
    run.add_argument("--save", action="store_true", help="persist the run in the database")
    run.add_argument("--quiet", action="store_true")

    sample = sub.add_parser("sample", help="generate a synthetic shipment workbook")
    sample.add_argument("--shipments", type=int, default=120)
    sample.add_argument("--plan-date", type=_parse_date, default=None)
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--out", type=Path, default=None)
    sample.add_argument(
        "--all-coords",
        action="store_true",
        help="fill every row's lat/lon (no Census geocoding needed to run it)",
    )

    sub.add_parser("osrm-check", help="check the OSRM server configured in OSRM_URL")
    sub.add_parser("clear-cache", help="clear the travel-time cache in the database")
    return ap


def settings_from_args(args: argparse.Namespace) -> RunSettings:
    pull = [int(x) for x in args.pull_forward.split(",") if x.strip()]
    extra: dict[str, object] = {}
    if args.max_loads is not None:
        extra["max_loads_per_truck"] = args.max_loads
    return RunSettings(
        profile=args.profile,
        plan_date=args.plan_date or hub_today() + dt.timedelta(days=2),
        preset=args.preset,
        solve_time_seconds=args.solve_time,
        pull_forward_days=pull,
        lunch_mode=args.lunch_mode,
        travel_provider=args.provider,
        truck_fixed_cost=args.fixed_cost,
        span_cost_coefficient=args.span_cost,
        **extra,  # type: ignore[arg-type]
    )


def format_plan(plan: Plan, stops: list[Stop]) -> str:
    """Human-readable plan summary (also used by tests)."""
    by_id = {s.stop_id: s for s in stops}
    k = plan.kpis
    lines = [
        f"RouteOpt plan {plan.run_id} v{plan.plan_version} for D={plan.plan_date.isoformat()} "
        f"(preset {plan.settings.get('preset')}, travel: {plan.matrix_source})",
        f"Solver: {plan.solver_stats.status} in {plan.solver_stats.wall_time_s}s | trucks used "
        f"{k.trucks_used}/{k.trucks_selected} | loads {k.loads} | stops served {k.stops_served} "
        f"| D stops {k.d_stops_served}/{k.d_stops_total} ({k.d_stops_served_pct}%) | unassigned "
        f"{k.unassigned_stops} | drive {k.total_drive_minutes:.0f} min / {k.total_miles:.0f} mi "
        f"| avg on duty {k.avg_on_duty_minutes:.0f} min | fleet volume use {k.fleet_volume_utilization_pct}%",
    ]
    for w in plan.warnings:
        lines.append(f"WARNING: {w}")
    for t in plan.used_trucks:
        lines.append(
            f"\n{t.number} {t.driver_name} - on duty {t.on_duty_minutes:.0f} min, lunch {t.lunch.where}"
        )
        for load in t.loads:
            if not load.stops:
                continue
            lines.append(
                f"  Load {load.seq}: depart {clock(load.depart_s)} return {clock(load.return_s)} "
                f"| load-out {load.load_out_volume:.0f} cu ft | {load.drive_minutes:.0f} min drive "
                f"| {load.distance_miles:.1f} mi"
            )
            for v in load.stops:
                s = by_id.get(v.stop_id)
                label = s.customer_label if s else v.stop_id
                addr = s.full_address if s else ""
                lines.append(
                    f"    #{v.seq} {clock(v.arrive_s)} ({clock(v.eta_window_start_s)}-"
                    f"{clock(v.eta_window_end_s)}) {label} | {addr} | vol after {v.volume_after:.0f}"
                    + (" | LUNCH before" if v.lunch_before else "")
                )
    if plan.unassigned:
        lines.append("\nUnassigned stops:")
        for u in plan.unassigned:
            s = by_id.get(u.stop_id)
            label = s.customer_label if s else u.stop_id
            lines.append(
                f"  {u.stop_id} tier {u.tier} {u.day_tag.value} {label} | {u.volume:.0f} cu ft | {u.reason}"
                + (f" ({u.detail})" if u.detail else "")
            )
    if plan.out_of_scope:
        lines.append(
            f"\nNot in scope (pull-forward off): {len(plan.out_of_scope)} stops "
            f"{dict(Counter(o.day_tag.value for o in plan.out_of_scope))}"
        )
    if plan.violations:
        lines.append("\nValidator:")
        for violation in plan.violations:
            lines.append(f"  {violation}")
    else:
        lines.append("\nValidator: no violations")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    from routeopt.pipeline import default_trucks, run_file

    app = AppConfig()
    settings = settings_from_args(args)
    trucks = default_trucks()
    if args.trucks:  # --trucks N wins over the profile's truck selection
        trucks = trucks[: args.trucks]
        settings = settings.model_copy(update={"trucks_selected": []})
    from routeopt.ingest.census_geocoder import CensusGeocoder

    try:
        result, ing, choice = run_file(
            args.workbook, settings, app=app, trucks=trucks, geocoder=CensusGeocoder()
        )
    except ValueError as exc:
        print(f"Cannot run: {exc}", file=sys.stderr)
        return 2
    plan = result.plan
    if choice.fallback_used:
        print(f"NOTE: {choice.reason} Using straight-line estimates.", file=sys.stderr)
    if not args.quiet:
        print(format_plan(plan, result.stops))
    if args.export:
        from routeopt.export.excel import export_excel
        from routeopt.export.route_sheets_pdf import export_route_sheets

        args.export.mkdir(parents=True, exist_ok=True)
        xlsx = export_excel(plan, result.stops, settings, args.export)
        pdf = export_route_sheets(plan, result.stops, settings, args.export)
        print(f"Exported {xlsx} and {pdf}")
    if args.save:
        from routeopt.db.repo import Repository

        repo = Repository(app.effective_db_url)
        repo.init_schema()
        repo.save_run(
            plan,
            result.all_stops,
            settings,
            result.full_matrix or result.matrix,
            ing.validation.shipments,
            trucks,
            source_name=str(args.workbook),
            source_bytes=Path(args.workbook).read_bytes(),
            warnings=[w.message for w in ing.validation.warnings],
            in_scope_ids=result.in_scope_ids,
        )
        print(f"Saved run {plan.run_id}")
    return 0 if plan.solver_stats.first_solution_found else 1


def cmd_sample(args: argparse.Namespace) -> int:
    from routeopt.sample_data import generate_sample

    plan_date = args.plan_date or hub_today() + dt.timedelta(days=2)
    out = args.out or Path("data/samples") / f"sample_shipments_{plan_date.isoformat()}.xlsx"
    path = generate_sample(
        args.shipments, plan_date, args.seed, out, blank_coords=not args.all_coords
    )
    print(f"Wrote {args.shipments} shipments for D={plan_date.isoformat()} to {path}")
    return 0


def cmd_osrm_check(_args: argparse.Namespace) -> int:
    from routeopt.travel.osrm import OSRMProvider

    app = AppConfig()
    ok = OSRMProvider(app.osrm_url, timeout_s=app.osrm_timeout_s).healthy()
    print(f"OSRM at {app.osrm_url}: {'healthy' if ok else 'NOT responding'}")
    return 0 if ok else 1


def cmd_clear_cache(_args: argparse.Namespace) -> int:
    from routeopt.db.repo import Repository

    repo = Repository(AppConfig().effective_db_url)
    repo.init_schema()
    n = repo.clear_travel_cache()
    print(f"Removed {n} cached travel matrices")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(AppConfig().log_level)
    args = build_parser().parse_args(argv)
    handlers = {
        "run": cmd_run,
        "sample": cmd_sample,
        "osrm-check": cmd_osrm_check,
        "clear-cache": cmd_clear_cache,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
