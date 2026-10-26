"""Printable PDF route sheets (brief 8.3): one page (or more) per load + fleet overview."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from routeopt.config import RunSettings
from routeopt.domain import SITE_BY_CODE, Load, Plan, PlanTruck, Stop, TruckConfig, clock
from routeopt.export.excel import export_filename
from routeopt.travel import FALLBACK_WARNING

_styles = getSampleStyleSheet()
_body = ParagraphStyle(
    "body", parent=_styles["BodyText"], fontName="Helvetica", fontSize=8, leading=10
)
_small = ParagraphStyle(
    "small", parent=_body, fontSize=7, leading=9, textColor=colors.HexColor("#444444")
)
_h1 = ParagraphStyle(
    "h1", parent=_styles["Heading1"], fontName="Helvetica-Bold", fontSize=14, spaceAfter=4
)
_h2 = ParagraphStyle(
    "h2", parent=_styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, spaceAfter=2
)
_warn = ParagraphStyle(
    "warn", parent=_body, textColor=colors.HexColor("#B00020"), fontName="Helvetica-Bold"
)


def _p(text: Any, style: ParagraphStyle = _body) -> Paragraph:
    safe = (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return Paragraph(safe, style)


def _footer_lines(plan: Plan) -> list[Paragraph]:
    lines: list[Paragraph] = []
    if plan.matrix_source != "osrm":
        lines.append(_p(FALLBACK_WARNING, _warn))
    if plan.export_override_reason:
        lines.append(_p(f"EXPORTED WITH OVERRIDE: {plan.export_override_reason}", _warn))
    return lines


def _overview(plan: Plan, settings: RunSettings) -> list[Any]:
    rows: list[list[Any]] = [
        [
            "Truck",
            "Driver",
            "Loads",
            "Stops",
            "Depart",
            "Return",
            "On duty (min)",
            "Load-out (cu ft)",
            "Lunch",
        ]
    ]
    for truck in plan.trucks:
        loads = [load for load in truck.loads if load.stops]
        if not loads:
            continue
        rows.append(
            [
                truck.number,
                _p(truck.driver_name),
                len(loads),
                sum(len(load.stops) for load in loads),
                clock(loads[0].depart_s),
                clock(loads[-1].return_s),
                f"{truck.on_duty_minutes:.0f}",
                " / ".join(f"{load.load_out_volume:.0f}" for load in loads),
                _p(truck.lunch.where, _small),
            ]
        )
    k = plan.kpis
    table = Table(
        rows,
        repeatRows=1,
        colWidths=[
            0.6 * inch,
            1.4 * inch,
            0.5 * inch,
            0.5 * inch,
            0.8 * inch,
            0.8 * inch,
            0.9 * inch,
            1.2 * inch,
            3.0 * inch,
        ],
    )
    table.setStyle(_table_style())
    return [
        _p(
            f"RouteOpt fleet overview - delivery day {plan.plan_date.isoformat()} - "
            f"run {plan.run_id} v{plan.plan_version}",
            _h1,
        ),
        _p(
            f"Trucks used {k.trucks_used}/{k.trucks_selected} | loads {k.loads} | stops {k.stops_served} | "
            f"D stops {k.d_stops_served}/{k.d_stops_total} | unassigned {k.unassigned_stops} | "
            f"drive {k.total_drive_minutes:.0f} min / {k.total_miles:.0f} mi | preset {plan.settings.get('preset', '')}"
        ),
        Spacer(1, 6),
        table,
        Spacer(1, 6),
        *_footer_lines(plan),
        PageBreak(),
    ]


def _table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE7F5")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FB")]),
        ]
    )


def _load_page(
    plan: Plan,
    truck: PlanTruck,
    load: Load,
    n_loads: int,
    by_id: dict[str, Stop],
    settings: RunSettings,
) -> list[Any]:
    header = [
        _p(
            f"{truck.number} - {truck.driver_name or 'driver not assigned'} - "
            f"delivery day {plan.plan_date.isoformat()}",
            _h1,
        ),
        _p(
            f"Load {load.seq} of {n_loads} from {SITE_BY_CODE[load.site].name}"
            + (" (pallet trip)" if load.pallet else "")
            + f" | depart {clock(load.depart_s)} | expected return {clock(load.return_s)} | "
            f"load-out {load.load_out_volume:.0f} cu ft, {load.load_out_weight:.0f} lb, {load.load_out_pieces} pieces"
            + (
                f" | reload from {clock(load.reload_start_s)}"
                if load.reload_start_s is not None
                else (
                    f" | loading from {clock(settings.shift_start_s)}"
                    if settings.first_load_minutes
                    else " | pre-loaded before the shift"
                )
            )
            + f" | lunch: {truck.lunch.where or 'during the reload'}",
            _h2,
        ),
    ]
    rows: list[list[Any]] = [
        [
            "#",
            "ETA",
            "Customer window",
            "Customer",
            "Appointment contact / phone",
            "Address",
            "Shipments",
            "Svc min",
            "Notes",
        ]
    ]
    lunch = truck.lunch
    lunch_row_after = (
        lunch.after_stop_seq if lunch.kind == "break" and lunch.load_seq == load.seq else None
    )
    lunch_rows: list[int] = []
    if lunch_row_after == 0:
        lunch_rows.append(len(rows))
        rows.append(
            [
                "",
                _p(
                    f"LUNCH {clock(lunch.start_s)}-{clock(lunch.end_s)} before the first stop",
                    _warn,
                ),
                *[""] * 7,
            ]
        )
    for visit in load.stops:
        stop = by_id.get(visit.stop_id)
        shipments = (
            "<br/>".join(
                f"{sh.model_code or ''} x {sh.pieces} ({sh.shipment_type.value})"
                for sh in (stop.shipments if stop else [])
            )
            or visit.stop_id
        )
        notes = "<br/>".join(sh.notes for sh in (stop.shipments if stop else []) if sh.notes)
        rows.append(
            [
                visit.seq,
                clock(visit.arrive_s),
                f"{clock(visit.eta_window_start_s)} - {clock(visit.eta_window_end_s)}",
                _p(stop.customer_label if stop else ""),
                _p(
                    " / ".join(x for x in (stop.contact, ", ".join(stop.phones)) if x)
                    if stop
                    else ""
                ),
                _p(f"{stop.address}, {stop.city} {stop.zip}" if stop else ""),
                Paragraph(shipments.replace("&", "&amp;"), _body),
                str(stop.service_total_minutes if stop else ""),
                Paragraph(notes.replace("&", "&amp;"), _small),
            ]
        )
        if lunch_row_after == visit.seq:
            lunch_rows.append(len(rows))
            rows.append(
                [
                    "",
                    _p(f"LUNCH {clock(lunch.start_s)}-{clock(lunch.end_s)} after this stop", _warn),
                    *[""] * 7,
                ]
            )
    table = Table(
        rows,
        repeatRows=1,
        colWidths=[
            0.3 * inch,
            0.7 * inch,
            1.3 * inch,
            1.5 * inch,
            1.0 * inch,
            1.9 * inch,
            1.6 * inch,
            0.8 * inch,
            1.4 * inch,
        ],
    )
    style = _table_style()
    for r in lunch_rows:
        style.add("SPAN", (1, r), (-1, r))
        style.add("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FFF3E0"))
    table.setStyle(style)
    footer = [
        Spacer(1, 6),
        _p(
            f"Totals: {len(load.stops)} stops | {load.drive_minutes:.0f} min driving | {load.distance_miles:.1f} mi | "
            f"return {clock(load.return_s)} | max volume on the road {load.max_volume_on_road:.0f} cu ft | "
            f"ETA windows are +/- {settings.eta_window_hours:g} h",
            _small,
        ),
        *_footer_lines(plan),
    ]
    return [*header, Spacer(1, 4), table, KeepTogether(footer), PageBreak()]


def export_route_sheets(
    plan: Plan,
    stops: list[Stop],
    settings: RunSettings,
    out_dir: Path,
    *,
    include_overview: bool = True,
    trucks_snapshot: list[TruckConfig] | None = None,
) -> Path:
    """Write the route-sheet PDF and return its path."""
    by_id = {s.stop_id: s for s in stops}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{export_filename(plan, '_routes')}.pdf"
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(letter),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=f"RouteOpt route sheets {plan.plan_date.isoformat()} {plan.run_id} v{plan.plan_version}",
    )
    story: list[Any] = []
    if include_overview:
        story.extend(_overview(plan, settings))
    for truck in plan.trucks:
        loads = [load for load in truck.loads if load.stops]
        for load in loads:
            story.extend(_load_page(plan, truck, load, len(loads), by_id, settings))
    if not story:
        story.append(_p("This plan has no routes.", _h1))
    if isinstance(story[-1], PageBreak):
        story.pop()
    doc.build(story)
    return path
