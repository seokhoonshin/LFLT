"""Excel export (brief 8.2): Summary, Routes, Stops, Unassigned, Trucks, Settings."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from routeopt.config import RunSettings
from routeopt.domain import SITE_BY_CODE, Plan, Stop, TruckConfig, seconds_to_time
from routeopt.travel import FALLBACK_WARNING
from routeopt.travel.tomtom import is_road_source, source_label

TIME_FMT = "h:mm AM/PM"
HEADER_FILL = PatternFill("solid", fgColor="DDE7F5")

ROUTES_COLUMNS = [
    "truck",
    "driver",
    "load",
    "stop_seq",
    "eta",
    "window_start",
    "window_end",
    "customer_name",
    "phone",
    "address",
    "city",
    "state",
    "zip",
    "latitude",
    "longitude",
    "shipment_id",
    "purchase_order",
    "delivery_date",
    "order_type",
    "model_code",
    "pieces",
    "volume_cuft",
    "weight_lb",
    "service_minutes",
    "notes",
    "shipment_type",
    "stop_id",
    "origin_dc",
    "pallet_required",
    "priority",
    "appointment_contact",
]


def _t(seconds: int | None) -> dt.time | None:
    return None if seconds is None else seconds_to_time(seconds)


def _write_table(
    ws: Worksheet,
    headers: list[str],
    rows: list[list[Any]],
    time_cols: set[str] | None = None,
    text_cols: set[str] | None = None,
) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append(row)
    time_idx = {headers.index(c) + 1 for c in (time_cols or set()) if c in headers}
    text_idx = {headers.index(c) + 1 for c in (text_cols or set()) if c in headers}
    for r in range(2, ws.max_row + 1):
        for c in time_idx:
            ws.cell(row=r, column=c).number_format = TIME_FMT
        for c in text_idx:
            ws.cell(row=r, column=c).number_format = "@"
    for i, header in enumerate(headers, start=1):
        width = len(str(header))
        for r in range(2, min(ws.max_row, 400) + 1):
            v = ws.cell(row=r, column=i).value
            if v is not None:
                width = max(width, len(str(v)))
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 50)
    ws.freeze_panes = "A2"


def _kv_sheet(ws: Worksheet, rows: list[tuple[str, Any]]) -> None:
    ws.append(["Item", "Value"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
    for k, v in rows:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 80
    ws.freeze_panes = "A2"


def _pretty(key: str) -> str:
    return key.replace("_", " ").capitalize()


def export_filename(plan: Plan, suffix: str = "") -> str:
    return f"routeopt_{plan.plan_date.isoformat()}_{plan.run_id}_v{plan.plan_version}{suffix}"


def export_excel(
    plan: Plan,
    stops: list[Stop],
    settings: RunSettings,
    out_dir: Path,
    *,
    trucks_snapshot: list[TruckConfig] | None = None,
    extra_warnings: list[str] | None = None,
    source_name: str = "",
) -> Path:
    """Write the plan workbook and return its path."""
    by_id = {s.stop_id: s for s in stops}
    wb = Workbook()
    k = plan.kpis

    # ---- Summary -----------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    warnings = list(plan.warnings) + list(extra_warnings or [])
    if not is_road_source(plan.matrix_source) and FALLBACK_WARNING not in warnings:
        warnings.insert(0, FALLBACK_WARNING)
    rows: list[tuple[str, Any]] = [
        ("Run", plan.run_id),
        ("Plan version", f"v{plan.plan_version}"),
        ("Delivery day (D)", plan.plan_date.isoformat()),
        ("Run day (D-2)", (plan.plan_date - dt.timedelta(days=2)).isoformat()),
        ("Created", plan.created_at.strftime("%Y-%m-%d %H:%M")),
        ("Source file", source_name),
        ("Preset", plan.settings.get("preset", "")),
        ("Travel times", source_label(plan.matrix_source)),
        ("", ""),
        ("Trucks used / selected", f"{k.trucks_used} / {k.trucks_selected}"),
        ("Loads", k.loads),
        ("Stops served", k.stops_served),
        ("Shipments served", k.shipments_served),
        (
            "D stops served / total",
            f"{k.d_stops_served} / {k.d_stops_total} ({k.d_stops_served_pct}%)",
        ),
        (
            "Pull-forward stops included",
            ", ".join(f"{d}: {n}" for d, n in k.pull_forward_included.items()) or "none",
        ),
        (
            "Unassigned stops by tier",
            ", ".join(f"{t}: {n}" for t, n in k.unassigned_by_tier.items()) or "none",
        ),
        ("Unassigned shipments", k.unassigned_shipments),
        ("Total drive minutes", k.total_drive_minutes),
        ("Total miles", k.total_miles),
        ("Average on-duty minutes", k.avg_on_duty_minutes),
        ("Longest on-duty minutes", k.max_on_duty_minutes),
        ("Fleet volume utilization %", k.fleet_volume_utilization_pct),
        ("Average stops per load", k.avg_stops_per_load),
        (
            "Delivery volume demand vs available (cu ft)",
            f"{k.demand_delivery_volume:,.0f} vs {k.available_volume:,.0f}",
        ),
        (
            "Service minutes demand vs available",
            f"{k.demand_service_minutes:,.0f} vs {k.available_minutes:,.0f}",
        ),
        ("", ""),
        ("Optimizer status", plan.solver_stats.status),
        ("Optimizer objective", plan.solver_stats.objective),
        ("Optimizer wall time (s)", plan.solver_stats.wall_time_s),
        ("Starting solution", plan.solver_stats.initial_solution),
        ("Fleet reduction", plan.solver_stats.fleet_reduction),
        ("", ""),
    ]
    rows.append(("WARNINGS", ""))
    rows.extend(("", w) for w in warnings)
    if plan.export_override_reason:
        rows.append(("EXPORTED WITH OVERRIDE", plan.export_override_reason))
    rows.append(("", ""))
    rows.append(
        (
            "VALIDATOR",
            "no violations" if not plan.violations else f"{len(plan.violations)} finding(s)",
        )
    )
    for v in plan.violations:
        where = " ".join(
            p for p in (v.truck or "", f"load {v.load}" if v.load else "", v.stop_id or "") if p
        )
        rows.append((f"{v.severity.upper()} {where}".strip(), v.message))
    _kv_sheet(ws, rows)

    # ---- Routes (one row per shipment) --------------------------------------------
    # ---- Load summary (TMS load-list layout) ----------------------------------------
    from routeopt.export.load_summary import COLUMNS, TIME_COLUMNS, load_summary_rows

    ws = wb.create_sheet("Load Summary")
    summary_rows = load_summary_rows(plan, stops, settings)
    _write_table(ws, COLUMNS, [[r[c] for c in COLUMNS] for r in summary_rows])
    for c in TIME_COLUMNS:
        col = COLUMNS.index(c) + 1
        for r in range(2, ws.max_row + 1):
            ws.cell(row=r, column=col).number_format = "m/d/yyyy h:mm"
        ws.column_dimensions[get_column_letter(col)].width = 17

    ws = wb.create_sheet("Routes")
    route_rows: list[list[Any]] = []
    for truck in plan.trucks:
        for load in truck.loads:
            for visit in load.stops:
                stop = by_id.get(visit.stop_id)
                if stop is None:
                    continue
                for sh in stop.shipments:
                    route_rows.append(
                        [
                            truck.number,
                            truck.driver_name,
                            load.seq,
                            visit.seq,
                            _t(visit.arrive_s),
                            _t(visit.eta_window_start_s),
                            _t(visit.eta_window_end_s),
                            sh.customer_name,
                            sh.phone,
                            sh.address,
                            sh.city,
                            sh.state,
                            sh.zip,
                            sh.latitude,
                            sh.longitude,
                            sh.shipment_id,
                            sh.purchase_order,
                            sh.delivery_date,
                            sh.order_type.value,
                            sh.model_code,
                            sh.pieces,
                            sh.volume_cuft,
                            sh.weight_lb,
                            sh.service_minutes,
                            sh.notes,
                            sh.shipment_type.value,
                            stop.stop_id,
                            sh.origin_dc or SITE_BY_CODE[sh.site].dc_codes[0],
                            "Y" if sh.pallet_required else "N",
                            sh.priority,
                            stop.contact,
                        ]
                    )
    _write_table(
        ws, ROUTES_COLUMNS, route_rows, {"eta", "window_start", "window_end"}, {"zip", "phone"}
    )
    for r in range(2, ws.max_row + 1):
        ws.cell(
            row=r, column=ROUTES_COLUMNS.index("delivery_date") + 1
        ).number_format = "yyyy-mm-dd"

    # ---- Stops (one row per stop visit) ---------------------------------------------
    ws = wb.create_sheet("Stops")
    stop_headers = [
        "truck",
        "driver",
        "load",
        "stop_seq",
        "stop_id",
        "customer",
        "address",
        "city",
        "zip",
        "arrive",
        "depart",
        "travel_min_from_prev",
        "dist_mi_from_prev",
        "stop_volume",
        "volume_on_arrival",
        "volume_after",
        "weight_after",
        "pieces_after",
        "service_minutes",
        "eta_window_start",
        "eta_window_end",
        "lunch_before",
        "tier",
        "day_tag",
        "shipments",
        "dc",
        "pallet_trip",
        "appointment_contact",
    ]
    stop_rows: list[list[Any]] = []
    for truck in plan.trucks:
        for load in truck.loads:
            for visit in load.stops:
                stop = by_id.get(visit.stop_id)
                stop_rows.append(
                    [
                        truck.number,
                        truck.driver_name,
                        load.seq,
                        visit.seq,
                        visit.stop_id,
                        stop.customer_label if stop else "",
                        stop.address if stop else "",
                        stop.city if stop else "",
                        stop.zip if stop else "",
                        _t(visit.arrive_s),
                        _t(visit.depart_s),
                        visit.travel_min_from_prev,
                        visit.dist_mi_from_prev,
                        round(visit.volume_on_arrival - visit.volume_after, 1),
                        visit.volume_on_arrival,
                        visit.volume_after,
                        visit.weight_after,
                        visit.pieces_after,
                        stop.service_total_minutes if stop else 0,
                        _t(visit.eta_window_start_s),
                        _t(visit.eta_window_end_s),
                        "Yes" if visit.lunch_before else "",
                        stop.tier if stop else "",
                        stop.day_tag.value if stop else "",
                        len(stop.shipments) if stop else 0,
                        SITE_BY_CODE[load.site].name,
                        "Yes" if load.pallet else "",
                        stop.contact if stop else "",
                    ]
                )
    _write_table(
        ws,
        stop_headers,
        stop_rows,
        {"arrive", "depart", "eta_window_start", "eta_window_end"},
        {"zip"},
    )

    # ---- Unassigned (shipment level) -------------------------------------------------
    ws = wb.create_sheet("Unassigned")
    un_headers = [
        "stop_id",
        "tier",
        "day_tag",
        "customer_name",
        "address",
        "city",
        "zip",
        "shipment_id",
        "shipment_type",
        "model_code",
        "pieces",
        "volume_cuft",
        "weight_lb",
        "service_minutes",
        "reason",
        "detail",
    ]
    un_rows: list[list[Any]] = []

    def _add_unassigned(entries: list[Any]) -> None:
        for u in entries:
            stop = by_id.get(u.stop_id)
            if stop is None:
                un_rows.append(
                    [
                        u.stop_id,
                        u.tier,
                        u.day_tag.value,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        u.volume,
                        "",
                        u.service_minutes,
                        u.reason,
                        u.detail,
                    ]
                )
                continue
            for sh in stop.shipments:
                un_rows.append(
                    [
                        u.stop_id,
                        u.tier,
                        u.day_tag.value,
                        sh.customer_name,
                        sh.address,
                        sh.city,
                        sh.zip,
                        sh.shipment_id,
                        sh.shipment_type.value,
                        sh.model_code,
                        sh.pieces,
                        sh.volume_cuft,
                        sh.weight_lb,
                        sh.service_minutes,
                        u.reason,
                        u.detail,
                    ]
                )

    _add_unassigned(plan.unassigned)
    if plan.out_of_scope:
        un_rows.append([])
        un_rows.append(["Not in scope (pull-forward off)"])
        _add_unassigned(plan.out_of_scope)
    _write_table(ws, un_headers, un_rows, text_cols={"zip"})

    # ---- Trucks ------------------------------------------------------------------------
    ws = wb.create_sheet("Trucks")
    truck_headers = [
        "truck",
        "driver",
        "used",
        "loads",
        "stops",
        "on_duty_minutes",
        "driver_start",
        "first_depart",
        "last_return",
        "lunch",
        "drive_minutes",
        "miles",
        "max_load_out_volume",
        "avg_load_utilization_pct",
    ]
    truck_rows: list[list[Any]] = []
    cap = settings.truck_volume_cuft
    for truck in plan.trucks:
        loads = [load for load in truck.loads if load.stops]
        utils = [100 * load.load_out_volume / cap for load in loads] if cap else []
        truck_rows.append(
            [
                truck.number,
                truck.driver_name,
                "Yes" if loads else "No",
                len(loads),
                sum(len(load.stops) for load in loads),
                truck.on_duty_minutes,
                _t(truck.start_s) if loads and truck.start_s is not None else None,
                _t(loads[0].depart_s) if loads else None,
                _t(loads[-1].return_s) if loads else None,
                truck.lunch.where,
                round(sum(load.drive_minutes for load in loads), 1),
                round(sum(load.distance_miles for load in loads), 1),
                max((load.load_out_volume for load in loads), default=0.0),
                round(sum(utils) / len(utils), 1) if utils else 0.0,
            ]
        )
    _write_table(ws, truck_headers, truck_rows, {"driver_start", "first_depart", "last_return"})

    # ---- Settings ------------------------------------------------------------------------
    ws = wb.create_sheet("Settings")
    setting_rows: list[tuple[str, Any]] = [
        ("Run", plan.run_id),
        ("Version", plan.plan_version),
        ("Created", plan.created_at.strftime("%Y-%m-%d %H:%M")),
        ("Travel provider used", plan.matrix_source),
    ]
    for key, value in plan.settings.items():
        setting_rows.append((_pretty(key), str(value) if isinstance(value, dict | list) else value))
    if trucks_snapshot:
        setting_rows.append(("", ""))
        setting_rows.append(("Trucks & drivers", ""))
        for t in trucks_snapshot:
            setting_rows.append(
                (
                    t.truck_number,
                    f"{t.driver_name or '(no driver)'}{'' if t.active else ' (inactive)'}",
                )
            )
    _kv_sheet(ws, setting_rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{export_filename(plan)}.xlsx"
    wb.save(path)
    return path
