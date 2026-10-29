"""Appointments - the trucks picked on Results; set each shipment's appointment status."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import io
from itertools import groupby

import altair as alt
import pandas as pd
import streamlit as st
from routeopt.appointments import OUTCOMES, STATUSES, outcome_summary, stop_outcomes

from _common import get_repo, osrm_status_line, setup_page

setup_page("Appointments")
osrm_status_line()
repo = get_repo()
OUTCOME_COLOR = {"Confirmed": "#0ca30c", "Declined": "#d03b3b", "Open": "#9a9a92"}
OUTCOME_ICON = {"Confirmed": "✅", "Declined": "⛔", "Open": "⏳"}

st.title("Appointments")
st.caption(
    "Trucks picked with **Select this truck** on Results. Set each shipment's status as you "
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

counts = {s: sum(r["status"] == s for r in day_rows) for s in STATUSES}
m = st.columns(len(STATUSES) + 1)
m[0].metric("Shipments", len(day_rows))
for col, status in zip(m[1:], STATUSES, strict=True):
    col.metric(status, counts[status])

buf = io.BytesIO()
pd.DataFrame(day_rows).drop(columns=["id"]).to_excel(buf, index=False, sheet_name="Appointments")
st.download_button(
    "Download this day (Excel)",
    buf.getvalue(),
    file_name=f"appointments_{day.isoformat()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

COLUMNS = {
    "stop_seq": "Stop",
    "shipment_id": "Shipment ID",
    "customer": "Customer",
    "address": "Address",
    "bill_to_code": "Bill to",
    "delivery_date": "Suggested date",
    "eta": "ETA",
    "window": "Window",
    "volume_cuft": "Volume",
    "miles_from_prev": "Miles from prev",
    "stop_transit_min": "Stop transit min",
    "poc": "POC",
    "status": "Status",
    "note": "Note",
}


def truck_key(r: dict) -> tuple[str, int, str]:
    return (r["run_id"], r["version"], r["truck"])


for (run_id, version, truck), group in groupby(day_rows, key=truck_key):
    items = list(group)
    done = sum(r["status"] == "Confirmed" for r in items)
    declined = sum(r["status"] == "Declined" for r in items)
    st.markdown(
        f"#### {truck} · {done}/{len(items)} confirmed"
        + (f" · {declined} declined" if declined else "")
    )
    st.caption(f"Run {run_id} · v{version} · from {items[0]['dc']}")
    with st.form(f"form-{run_id}-{truck}"):
        edited: dict[int, pd.DataFrame] = {}
        for load_seq, load_items in groupby(items, key=lambda r: r["load_seq"]):
            load_items = list(load_items)
            st.markdown(f"**Load {load_seq}** — {load_items[0]['dc']}")
            df = pd.DataFrame(load_items).set_index("id")[list(COLUMNS)].rename(columns=COLUMNS)
            edited[load_seq] = st.data_editor(
                df,
                hide_index=True,
                use_container_width=True,
                disabled=[c for c in COLUMNS.values() if c not in ("Status", "Note")],
                column_config={
                    "Status": st.column_config.SelectboxColumn(
                        "Status",
                        options=STATUSES,
                        required=True,
                        help="Declined = the customer can't take it that day (dropped).",
                    ),
                    "Note": st.column_config.TextColumn("Note"),
                    "Suggested date": st.column_config.DateColumn(
                        "Suggested date", format="MM/DD/YYYY"
                    ),
                },
                key=f"ed-{run_id}-{truck}-{load_seq}",
            )
            # data_editor keeps the index (row id) even when hidden
            edited[load_seq].index = df.index
        if st.form_submit_button(f"Save {truck}", type="primary"):
            changes = {
                int(row_id): {"status": str(r["Status"]), "note": str(r["Note"] or "")}
                for frame in edited.values()
                for row_id, r in frame.iterrows()
            }
            n = repo.update_appointments(changes)
            st.toast(f"{truck}: {n} change(s) saved.")
            st.rerun()
    with st.expander(f"Remove {truck} from Appointments"):
        st.caption(
            "Only for a truck picked by mistake: removes its rows and statuses from the "
            "history. A stop the customer can't take is **Declined**, not removed."
        )
        if st.button(f"Remove {truck}", key=f"rm-{run_id}-{truck}"):
            repo.unselect_truck(run_id, truck)
            st.rerun()
