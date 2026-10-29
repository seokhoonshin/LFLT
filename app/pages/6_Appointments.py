"""Appointments - the trucks picked on Results; set each shipment's appointment status."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import datetime as dt
from itertools import groupby

import altair as alt
import pandas as pd
import streamlit as st
from routeopt.appointments import (
    EXPORT_COLUMNS,
    OUTCOMES,
    STATUSES,
    export_stops_by_load,
    group_by_stop,
    origin_label,
    outcome_summary,
    stop_outcomes,
    warehouse_address,
    warehouse_label,
)

from _common import get_repo, osrm_status_line, setup_page

setup_page("Appointments")
osrm_status_line()
repo = get_repo()
OUTCOME_COLOR = {"Confirmed": "#0ca30c", "Declined": "#d03b3b", "Open": "#9a9a92"}
OUTCOME_ICON = {"Confirmed": "✅", "Declined": "⛔", "Open": "⏳"}

st.title("Appointments")
st.caption(
    "Trucks picked with **Select this truck** on Results. Set each stop's status as you "
    "request and confirm appointments (**Declined** = the customer can't take it that day), "
    "then press **Save**. Everything is kept as history."
)

rows = repo.list_appointments()
if not rows:
    st.info("No trucks picked yet — open **Results** and press **Select this truck**.")
    st.stop()

# ---- cumulative results: every picked truck, all days --------------------------------
st.subheader("Results so far (all picked trucks)")
all_stops = stop_outcomes(rows)
first, last = min(r["delivery_date"] for r in rows), max(r["delivery_date"] for r in rows)
span = st.date_input("Delivery days", (first, last), min_value=first, max_value=last)
lo, hi = (span[0], span[-1]) if isinstance(span, tuple) and span else (first, last)
in_span = [x for x in all_stops if lo <= x["delivery_date"] <= hi]
summary = outcome_summary(in_span)
c = st.columns(4)
c[0].metric("Stops picked", len(in_span), help="Stops on the trucks picked on Results.")
for col, outcome in zip(c[1:], OUTCOMES, strict=True):
    o = summary[outcome]
    col.metric(
        f"{OUTCOME_ICON[outcome]} {outcome}",
        f"{o['stops']:.0f} ({o['stops_pct']:.0f}%)",
        help=f"{o['volume']:,.0f} cu ft ({o['volume_pct']:.0f}% of the picked volume)",
    )
st.caption(
    "A stop is **Confirmed** when every shipment on it is confirmed, **Declined** when any "
    "shipment is declined (the customer can't take it that day), otherwise **Open**. "
    f"Volume: confirmed {summary['Confirmed']['volume']:,.0f} cu ft "
    f"({summary['Confirmed']['volume_pct']:.0f}%), declined {summary['Declined']['volume']:,.0f} cu ft "
    f"({summary['Declined']['volume_pct']:.0f}%)."
)
if in_span:
    by_day = (
        pd.DataFrame(in_span)
        .groupby(["delivery_date", "outcome"])
        .agg(stops=("outcome", "size"), volume=("volume_cuft", "sum"))
        .reset_index()
    )
    by_day["share"] = by_day["stops"] / by_day.groupby("delivery_date")["stops"].transform("sum")
    by_day["day"] = pd.to_datetime(by_day["delivery_date"]).dt.strftime("%m/%d")
    by_day["volume"] = by_day["volume"].round(0)
    by_day["outcome_order"] = by_day["outcome"].map(OUTCOMES.index)
    chart = (
        alt.Chart(by_day)
        .mark_bar(size=22, stroke=None)
        .encode(
            x=alt.X("day:N", title="Delivery day", sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("stops:Q", title="Stops", axis=alt.Axis(tickMinStep=1, grid=True)),
            color=alt.Color(
                "outcome:N",
                title=None,
                scale=alt.Scale(domain=OUTCOMES, range=[OUTCOME_COLOR[o] for o in OUTCOMES]),
                legend=alt.Legend(orient="top"),
            ),
            order=alt.Order("outcome_order:Q"),
            tooltip=[
                alt.Tooltip("day:N", title="Day"),
                alt.Tooltip("outcome:N", title="Outcome"),
                alt.Tooltip("stops:Q", title="Stops"),
                alt.Tooltip("share:Q", title="Share of the day", format=".0%"),
                alt.Tooltip("volume:Q", title="cu ft", format=",.0f"),
            ],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)
    with st.expander("Table view"):
        table = by_day.pivot_table(
            index="delivery_date", columns="outcome", values="stops", fill_value=0
        ).reindex(columns=OUTCOMES, fill_value=0)
        table["Total"] = table.sum(axis=1)
        for o in ("Confirmed", "Declined"):
            table[f"{o} %"] = (100 * table[o] / table["Total"]).round(0)
        st.dataframe(table, use_container_width=True)

# ---- one day: set statuses -------------------------------------------------------------
st.subheader("Set statuses")
dates = sorted({r["delivery_date"] for r in rows}, reverse=True)
day = st.selectbox("Delivery day", dates, format_func=lambda d: d.strftime("%a %Y-%m-%d"))
day_rows = [r for r in rows if r["delivery_date"] == day]

view = st.radio(
    "View",
    ["By stop", "By shipment"],
    horizontal=True,
    help="By stop: shipments to the same stop are one row; a status set on it applies to "
    "every shipment of that stop.",
)
by_stop = view == "By stop"

counted = group_by_stop(day_rows) if by_stop else day_rows
m = st.columns(len(STATUSES) + 1)
m[0].metric("Stops" if by_stop else "Shipments", len(counted))
for col, status in zip(m[1:], STATUSES, strict=True):
    col.metric(status, sum(r["status"] == status for r in counted))

st.download_button(
    "Download all loads of this day (Excel)",
    export_stops_by_load(day_rows),
    file_name=f"appointments_{day.isoformat()}_all_loads.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    help="Per load: the warehouse as stop 1 (load total volume), then its stops. "
    "Declined stops are left out. Each load also has its own button below.",
)

# on screen: the export columns, then shipments and status
LABELS = [*EXPORT_COLUMNS, "Shipments", "Shipment IDs", "Status"]
EDITABLE = ("load ID", "APPT time", "APPT #", "Note", "Status")
if by_stop:
    EDITABLE = ("Stop", *EDITABLE)  # the order can be changed too (type new numbers)


def table_rows(load_items: list[dict]) -> list[dict]:
    """The warehouse (stop 1) and then the stops, with the on-screen labels."""
    first = load_items[0]
    load_id = next((r["load_id"] for r in load_items if r.get("load_id")), None)
    total = sum(r["stop_volume"] for r in {(r["stop_seq"]): r for r in load_items}.values())
    out = [
        {
            "load ID": load_id,
            "Stop": 1,
            "date": first["delivery_date"],
            "APPT time": None,
            "APPT #": None,
            "Customer": warehouse_label(first["dc"]),
            "Address": warehouse_address(first["dc"]),
            "Volume": f"Load total volume : {total:,.1f}",
            "Miles from prev": None,
            "POC": None,
            "Note": None,
            "Shipments": None,
            "Shipment IDs": None,
            "Status": None,
        }
    ]
    for r in load_items:
        out.append(
            {
                "load ID": r.get("load_id"),
                "Stop": r["stop_seq"] + 1,
                "date": r["delivery_date"],
                "APPT time": _to_time(r.get("appt_time")),
                "APPT #": r.get("appt_num"),
                "Customer": r["customer"],
                "Address": r["address"],
                "Volume": f"{r['stop_volume']:,.1f}",
                "Miles from prev": r["miles_from_prev"],
                "POC": r["poc"],
                "Note": r["note"],
                "Shipments": r["shipments"] if by_stop else 1,
                "Shipment IDs": r["shipment_ids"] if by_stop else r["shipment_id"],
                "Status": r["status"],
            }
        )
    return out


def _clean(v: object) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v).strip() or None


def _to_time(v: object) -> dt.time | None:
    if not v or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return dt.datetime.strptime(str(v), "%H:%M").time()
    except ValueError:
        return None


def _from_time(v: object) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    if isinstance(v, str):
        return v[:5] or None
    return v.strftime("%H:%M")  # type: ignore[attr-defined]


def truck_key(r: dict) -> tuple[str, int, str]:
    return (r["run_id"], r["version"], r["truck"])


for (run_id, version, truck), group in groupby(day_rows, key=truck_key):
    items = list(group)
    stops_here = group_by_stop(items)
    done = sum(s["status"] == "Confirmed" for s in stops_here)
    declined = sum(s["status"] == "Declined" for s in stops_here)
    st.markdown(
        f"#### {truck} · {done}/{len(stops_here)} stops confirmed"
        + (f" · {declined} declined" if declined else "")
    )
    st.caption(
        f"Run {run_id} · v{version} · from {origin_label(items[0]['dc'])} · "
        f"{len(stops_here)} stops, {len(items)} shipments"
    )
    stop_vol = {(s["load_seq"], s["stop_seq"]): s["volume_cuft"] for s in stops_here}
    shown = [
        {**r, "stop_volume": stop_vol[(r["load_seq"], r["stop_seq"])]}
        for r in (stops_here if by_stop else items)
    ]
    with st.form(f"form-{run_id}-{truck}"):
        edited: list[tuple[pd.DataFrame, list[list[int]]]] = []
        load_seqs: list[int] = []
        for load_seq, load_items in groupby(shown, key=lambda r: r["load_seq"]):
            load_items = list(load_items)
            load_seqs.append(load_seq)
            st.markdown(f"**Load {load_seq}** — from {origin_label(load_items[0]['dc'])}")
            df = pd.DataFrame(table_rows(load_items), columns=LABELS)
            ids = [[]] + [r["ids"] if by_stop else [r["id"]] for r in load_items]
            out = st.data_editor(
                df,
                hide_index=True,
                use_container_width=True,
                disabled=[c for c in LABELS if c not in EDITABLE],
                column_config={
                    "load ID": st.column_config.TextColumn(
                        "load ID",
                        help="TMS load number. Type it once (any row) and Save: the load's "
                        "empty rows get the same number.",
                    ),
                    "Stop": st.column_config.NumberColumn(
                        "Stop",
                        min_value=2,
                        step=1,
                        help="Stop 1 is the warehouse. Type new numbers to change the order "
                        "of the stops (by-stop view), then Save.",
                    ),
                    "date": st.column_config.DateColumn("date", format="MM/DD/YYYY"),
                    "APPT time": st.column_config.TimeColumn(
                        "APPT time", format="h:mm a", step=300
                    ),
                    "APPT #": st.column_config.TextColumn("APPT #"),
                    "Note": st.column_config.TextColumn("Note"),
                    "Shipment IDs": st.column_config.TextColumn("Shipment IDs", width="medium"),
                    "Status": st.column_config.SelectboxColumn(
                        "Status",
                        options=STATUSES,
                        help="Declined = the customer can't take it that day (dropped).",
                    ),
                },
                key=f"ed-{view}-{run_id}-{truck}-{load_seq}",
            )
            edited.append((out.reset_index(drop=True), ids))
        if st.form_submit_button(f"Save {truck}", type="primary"):
            changes: dict[int, dict[str, object]] = {}
            for frame, ids in edited:
                records = list(zip(frame.to_dict("records"), ids, strict=True))
                typed = {_clean(r["load ID"]) for r, _ids in records} - {None}
                fill = typed.pop() if len(typed) == 1 else None  # one number -> whole load
                stops_only = records[1:]  # row 0 is the warehouse
                # new stop order: by the typed number, ties keep the old order; then 1..n
                order = sorted(
                    range(len(stops_only)),
                    key=lambda i: (float(stops_only[i][0]["Stop"] or 0), i),
                )
                new_seq = {i: pos + 1 for pos, i in enumerate(order)}
                for i, (r, row_ids) in enumerate(stops_only):
                    values: dict[str, object] = {
                        "status": str(r["Status"] or STATUSES[0]),
                        "note": _clean(r["Note"]) or "",
                        "appt_num": _clean(r["APPT #"]),
                        "appt_time": _from_time(r["APPT time"]),
                        "load_id": _clean(r["load ID"]) or fill,
                    }
                    if by_stop:
                        values["stop_seq"] = new_seq[i]
                    for row_id in row_ids:
                        changes[int(row_id)] = values
            n = repo.update_appointments(changes)
            st.toast(f"{truck}: {n} change(s) saved.")
            st.rerun()
    dl = st.columns(max(len(load_seqs), 1) + 2)
    for col, load_seq in zip(dl, load_seqs, strict=False):
        col.download_button(
            f"Download {truck} · Load {load_seq}",
            export_stops_by_load(day_rows, only=(run_id, truck, load_seq)),
            file_name=f"appointments_{day.isoformat()}_{truck.split()[0]}_L{load_seq}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl-{run_id}-{truck}-{load_seq}",
        )
    with st.expander(f"Remove {truck} from Appointments"):
        st.caption(
            "Only for a truck picked by mistake: removes its rows and statuses from the "
            "history. A stop the customer can't take is **Declined**, not removed."
        )
        if st.button(f"Remove {truck}", key=f"rm-{run_id}-{truck}"):
            repo.unselect_truck(run_id, truck)
            st.rerun()
