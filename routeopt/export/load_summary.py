"""Load summary in the TMS load-list layout: one row per planned load (Excel sheet + PDF).

LOAD_ID and APPT_# stay blank (a planned load has no TMS number yet).  PICK_APT is when
loading starts (the driver start for load 1, the reload start for a later load) and
DROP_APT the ETA at the load's last stop.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from routeopt.config import RunSettings
from routeopt.domain import Plan, Stop

COLUMNS = [
    "LOAD_ID",
    "CARR_CD",
    "CUST_NAME",
    "TRUCK_NUM",
    "REMARK",
    "APPT_#",
    "PICK_UP",
    "LAST_DROP",
    "PICK_APT",
    "DROP_APT",
    "STATUS",
    "DISTANCE",
    "VOLUME",
    "DECLARED VAL",
    "SERVICE CODE",
    "# PIECES",
]
TIME_COLUMNS = {"PICK_APT", "DROP_APT"}
CARRIER_CODE = "UFPL"
SERVICE_CODE = "LFLT"
STATUS = "Planned"
WAREHOUSE_CODES = {"LGC2": "N1F", "LGC7": "NF4", "LGC8": "NF5"}  # TMS pick-up codes


def truck_code(number: str, position: int) -> str:
    """T01 -> UFPL_STL_TRK_01 (the truck's own number, else its position in the fleet)."""
    digits = re.findall(r"\d+", number)
    n = int(digits[-1]) if digits else position
    return f"{CARRIER_CODE}_STL_TRK_{n:02d}"


def _at(day: dt.date, seconds: int | None) -> dt.datetime | None:
    if seconds is None:
        return None
    return dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=int(seconds))


def load_summary_rows(plan: Plan, stops: list[Stop], settings: RunSettings) -> list[dict[str, Any]]:
    by_id = {s.stop_id: s for s in stops}
    rows: list[dict[str, Any]] = []
    for position, truck in enumerate(plan.trucks, start=1):
        for load in truck.loads:
            if not load.stops:
                continue
            last = by_id.get(load.stops[-1].stop_id)
            n_stops = len(load.stops)
            if load.seq == 1 or load.reload_start_s is None:
                loading = truck.start_s if truck.start_s is not None else settings.shift_start_s
            else:
                loading = load.reload_start_s
            declared = sum(
                sh.declared_value
                for visit in load.stops
                for sh in (by_id[visit.stop_id].shipments if visit.stop_id in by_id else [])
            )
            rows.append(
                {
                    "LOAD_ID": "",
                    "CARR_CD": CARRIER_CODE,
                    "CUST_NAME": last.customer_label if last else "",
                    "TRUCK_NUM": truck_code(truck.number, position),
                    "REMARK": "STL - LIVELOAD - "
                    + ("PALLET" if load.pallet else "FLOORLOAD")
                    + (f" - {n_stops} STOP" if n_stops > 1 else ""),
                    "APPT_#": "",
                    "PICK_UP": WAREHOUSE_CODES.get(load.site, load.site),
                    "LAST_DROP": (last.city or "").upper() if last else "",
                    "PICK_APT": _at(plan.plan_date, loading),
                    "DROP_APT": _at(plan.plan_date, load.stops[-1].arrive_s),
                    "STATUS": STATUS,
                    "DISTANCE": load.one_way_miles,
                    "VOLUME": round(load.load_out_volume, 4),
                    "DECLARED VAL": round(declared, 2),
                    "SERVICE CODE": SERVICE_CODE,
                    "# PIECES": load.load_out_pieces,
                }
            )
    return rows


def _text(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return f"{value.month}/{value.day}/{value.year} {value.hour}:{value.minute:02d}"
    if value is None:
        return ""
    return str(value)


def export_load_summary_pdf(
    plan: Plan, stops: list[Stop], settings: RunSettings, out_dir: Path
) -> Path:
    """One landscape table of every planned load, in the TMS load-list layout."""
    from routeopt.export.excel import export_filename

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (export_filename(plan, "_load_summary") + ".pdf")
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7, leading=8.5)
    head = ParagraphStyle(
        "head",
        parent=cell,
        fontName="Helvetica-Bold",
        fontSize=6,
        leading=7.5,
        textColor=colors.white,
    )

    def p(text: str, style: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(text.replace("&", "&amp;").replace("<", "&lt;"), style)

    data: list[list[Any]] = [[p(c, head) for c in COLUMNS]]
    for row in load_summary_rows(plan, stops, settings):
        data.append([p(_text(row[c])) for c in COLUMNS])
    # inches; headers at 6 pt fit on one line, and the row fits landscape letter
    widths = [0.45, 0.5, 1.2, 1.0, 1.1, 0.4, 0.45, 0.7, 0.85, 0.85, 0.45, 0.55, 0.5, 0.55, 0.5, 0.4]
    margin = 0.25 * inch
    usable = landscape(letter)[0] - 2 * margin
    scale = min(1.0, usable / (sum(widths) * inch))
    table = Table(data, colWidths=[w * inch * scale for w in widths], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
            ]
        )
    )
    title = Paragraph(
        f"Load summary - delivery day {plan.plan_date.isoformat()} - run {plan.run_id} "
        f"v{plan.plan_version}",
        styles["Heading2"],
    )
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(letter),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
    )
    doc.build([title, Spacer(1, 6), table])
    return path
