"""Analyze - dashboards: appointment results over time and the Plan 7774 aging pivots."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import altair as alt
import pandas as pd
import streamlit as st
from routeopt.analyze import (
    DROP_BUCKETS,
    RDD_BUCKETS,
    STATUSES,
    aging_pivot,
    daily_trend,
    detect_level,
    in_pg,
    prepare,
    read_upload,
    snapshot_day,
    summary_table,
    to_records,
)
from routeopt.appointments import OUTCOMES, outcome_summary, period_key, stop_outcomes
from routeopt.domain import hub_today

from _common import get_repo, osrm_status_line, setup_page

setup_page("Analyze")
osrm_status_line()
repo = get_repo()
OUTCOME_COLOR = {"Confirmed": "#0ca30c", "Declined": "#d03b3b", "Open": "#9a9a92"}
OUTCOME_ICON = {"Confirmed": "✅", "Declined": "⛔", "Open": "⏳"}
# categorical slots in fixed order (Hold, Requested); buckets: one hue, light -> dark
STATUS_COLOR = {"Hold": "#2a78d6", "Requested": "#eb6834"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]
BUCKET_BLUES = ["#cfe0f6", "#9cc1ec", "#6aa1e2", "#3b82d6", "#225fab", "#9a9a92"]

st.title("Analyze")
st.caption("Dashboards: appointment results over time, and the Plan 7774 aging of open loads.")


def is_reopt(r: dict) -> bool:
    return r.get("stage") == "reoptimized"


def appointment_results(rows: list[dict]) -> None:
    """How many picked stops were confirmed / declined / still open, by day / week / month
    / year (moved here from Appointments)."""
    orig_rows = [r for r in rows if not is_reopt(r)]
    reopt_rows = [r for r in rows if is_reopt(r)]
    orig_keys = {(r["run_id"], r["shipment_id"]) for r in orig_rows}
    # the record: every originally picked stop (declined included) + stops a re-plan added
    stat_rows = orig_rows + [
        r for r in reopt_rows if (r["run_id"], r["shipment_id"]) not in orig_keys
    ]

    # ---- cumulative results: every picked truck, all days --------------------------------
    all_stops = stop_outcomes(stat_rows)
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
    period = st.radio(
        "Group by",
        ["Day", "Week", "Month", "Year"],
        horizontal=True,
        help="Week = Monday to Sunday. Use Delivery days above to filter the range.",
    )
    if in_span:
        frame = pd.DataFrame(in_span)
        keys = [period_key(d, period) for d in frame["delivery_date"]]
        frame["start"] = [k[0] for k in keys]
        frame["period"] = [k[1] for k in keys]
        by_p = (
            frame.groupby(["start", "period", "outcome"])
            .agg(stops=("outcome", "size"), volume=("volume_cuft", "sum"))
            .reset_index()
            .sort_values("start")
        )
        by_p["share"] = by_p["stops"] / by_p.groupby("period")["stops"].transform("sum")
        by_p["volume"] = by_p["volume"].round(0)
        by_p["outcome_order"] = by_p["outcome"].map(OUTCOMES.index)
        order = list(dict.fromkeys(by_p["period"]))
        chart = (
            alt.Chart(by_p)
            .mark_bar(size=22 if len(order) < 25 else 10, stroke=None)
            .encode(
                x=alt.X("period:N", title=period, sort=order, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("stops:Q", title="Stops", axis=alt.Axis(tickMinStep=1, grid=True)),
                color=alt.Color(
                    "outcome:N",
                    title=None,
                    scale=alt.Scale(domain=OUTCOMES, range=[OUTCOME_COLOR[o] for o in OUTCOMES]),
                    legend=alt.Legend(orient="top"),
                ),
                order=alt.Order("outcome_order:Q"),
                tooltip=[
                    alt.Tooltip("period:N", title=period),
                    alt.Tooltip("outcome:N", title="Outcome"),
                    alt.Tooltip("stops:Q", title="Stops"),
                    alt.Tooltip("share:Q", title=f"Share of the {period.lower()}", format=".0%"),
                    alt.Tooltip("volume:Q", title="cu ft", format=",.0f"),
                ],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)
        with st.expander("Table view"):
            table = (
                by_p.pivot_table(index="period", columns="outcome", values="stops", fill_value=0)
                .reindex(index=order)
                .reindex(columns=OUTCOMES, fill_value=0)
            )
            table["Total"] = table.sum(axis=1)
            for o in ("Confirmed", "Declined"):
                table[f"{o} %"] = (100 * table[o] / table["Total"]).round(0)
            vol = by_p.pivot_table(index="period", columns="outcome", values="volume", fill_value=0)
            table["Confirmed cu ft"] = vol.reindex(index=order).get("Confirmed", 0)
            st.dataframe(table, use_container_width=True)


# ---- aging --------------------------------------------------------------------------------
def money(v: float) -> str:
    return f"${v:,.0f}"


def show_table(t: pd.DataFrame, hide_index: bool = False) -> None:
    fmt = {c: "{:,.0f}" for c in t.columns if pd.api.types.is_numeric_dtype(t[c])}
    if "Order Value" in t.columns:
        fmt["Order Value"] = "${:,.0f}"
    st.dataframe(t.style.format(fmt), use_container_width=True, hide_index=hide_index)


def aging_view(level: str) -> None:
    stored = repo.list_aging(level)
    if not stored:
        st.info(
            f"No {level}-level upload yet: run the Plan 7774 aging query "
            f"({'shipment' if level == 'shipment' else 'load'} version) and upload the result above."
        )
        return
    df = prepare(repo.load_aging(level))
    days = sorted(df["SNAPSHOT_DATE"].unique(), reverse=True)
    c1, c2 = st.columns([1, 3])
    day = c1.selectbox(
        "Snapshot day", days, format_func=lambda d: d.strftime("%a %Y-%m-%d"), key=f"day-{level}"
    )
    origins = sorted(df["ORIGIN_LOCATION_ID"].unique())
    pick = c2.multiselect("Origin", origins, default=origins, key=f"org-{level}")
    cur = df[(df["SNAPSHOT_DATE"] == day) & df["ORIGIN_LOCATION_ID"].isin(pick)]
    if cur.empty:
        st.warning("Nothing for this day and origin.")
        return
    k = st.columns(4)
    k[0].metric("Loads", f"{cur['LOAD_ID'].nunique():,}")
    k[1].metric("Volume (cu ft)", f"{cur['VOLUME'].sum():,.0f}")
    k[2].metric("Order value", money(cur["ORDER_VALUE"].sum()) if "ORDER_VALUE" in cur else "-")
    req = cur[cur["LOAD_STATUS"] == "Requested"]["VOLUME"].sum()
    k[3].metric("Requested share (volume)", f"{100 * req / max(cur['VOLUME'].sum(), 1):.0f}%")

    st.markdown("##### Summary — Origin × Hold / Requested")
    show_table(summary_table(cur, level), hide_index=True)

    a, b = st.columns(2)
    with a:
        st.markdown("##### PG 20 — volume by RDD aging")
        t = aging_pivot(in_pg(cur, {"20"}), "RDD_AGING_BUCKET")
        if t.empty:
            st.caption("No PG 20 rows.")
        else:
            show_table(t)
    with b:
        st.markdown("##### PG 30 & 50 — volume by Drop in TMS aging")
        t = aging_pivot(in_pg(cur, {"30", "50"}), "DROP_AGING_BUCKET")
        if t.empty:
            st.caption("No PG 30 / 50 rows.")
        else:
            show_table(t)
    if level == "load":
        st.caption("A load with mixed plan groups counts in every table its groups match.")

    st.markdown("##### Daily trend")
    t1, t2 = st.columns([1, 1])
    split = t1.selectbox(
        "Split by",
        ["LOAD_STATUS", "ORIGIN_LOCATION_ID", "DROP_AGING_BUCKET", "RDD_AGING_BUCKET", "PG"],
        format_func=lambda c: {
            "LOAD_STATUS": "Hold / Requested",
            "ORIGIN_LOCATION_ID": "Origin",
            "DROP_AGING_BUCKET": "Drop in TMS aging",
            "RDD_AGING_BUCKET": "RDD aging",
            "PG": "Plan group",
        }[c],
        key=f"split-{level}",
    )
    value = t2.selectbox(
        "Value",
        [c for c in ("VOLUME", "ORDER_VALUE") if c in df.columns],
        format_func=lambda c: {"VOLUME": "Volume (cu ft)", "ORDER_VALUE": "Order value ($)"}[c],
        key=f"val-{level}",
    )
    trend = daily_trend(df[df["ORIGIN_LOCATION_ID"].isin(pick)], split, value)
    if split == "LOAD_STATUS":
        domain, colors = STATUSES, [STATUS_COLOR[s] for s in STATUSES]
    elif split in ("DROP_AGING_BUCKET", "RDD_AGING_BUCKET"):
        domain = DROP_BUCKETS if split == "DROP_AGING_BUCKET" else RDD_BUCKETS
        colors = BUCKET_BLUES
    else:
        domain = sorted(trend[split].unique())
        colors = SERIES[: len(domain)] if len(domain) <= len(SERIES) else None
    trend["order"] = trend[split].map({d: i for i, d in enumerate(domain)})
    title = "Volume (cu ft)" if value == "VOLUME" else "Order value ($)"
    chart = (
        alt.Chart(trend)
        .mark_bar(size=18 if trend["SNAPSHOT_DATE"].nunique() < 30 else 8)
        .encode(
            x=alt.X(
                "yearmonthdate(SNAPSHOT_DATE):O",
                title="Snapshot day",
                axis=alt.Axis(labelAngle=0, format="%m/%d"),
            ),
            y=alt.Y(f"{value}:Q", title=title, axis=alt.Axis(grid=True)),
            color=alt.Color(
                f"{split}:N",
                title=None,
                scale=alt.Scale(domain=domain, range=colors) if colors else alt.Undefined,
                legend=alt.Legend(orient="top"),
            ),
            order=alt.Order("order:Q"),
            tooltip=[
                alt.Tooltip("yearmonthdate(SNAPSHOT_DATE):T", title="Day", format="%m/%d/%Y"),
                alt.Tooltip(f"{split}:N", title="Group"),
                alt.Tooltip(f"{value}:Q", title=title, format=",.0f"),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)

    with st.expander("Build your own pivot"):
        dims = [
            c
            for c in (
                "ORIGIN_LOCATION_ID",
                "LOAD_STATUS",
                "PG",
                "DROP_AGING_BUCKET",
                "RDD_AGING_BUCKET",
                "SERVICE_CODE",
                "MAIN_CUSTOMER" if level == "load" else "CUSTOMER",
                "MAIN_DEST_CITY" if level == "load" else "DEST_CITY",
            )
            if c in cur.columns
        ]
        p1, p2, p3 = st.columns(3)
        rows_by = p1.multiselect("Rows", dims, default=dims[:2], key=f"pr-{level}")
        col_by = p2.selectbox(
            "Columns", ["(none)", *dims], index=4 if len(dims) > 3 else 0, key=f"pc-{level}"
        )
        measure = p3.selectbox(
            "Value",
            ["VOLUME", "ORDER_VALUE", "Loads"] + (["Shipments"] if level == "shipment" else []),
            key=f"pv-{level}",
        )
        if rows_by:
            values, func = (
                ("LOAD_ID", "nunique")
                if measure == "Loads"
                else ("SHIPMENT_ID", "count")
                if measure == "Shipments"
                else (measure, "sum")
            )
            t = pd.pivot_table(
                cur,
                index=rows_by,
                columns=None if col_by == "(none)" else col_by,
                values=values,
                aggfunc=func,
                fill_value=0,
                observed=True,
                margins=True,
                margins_name="Grand Total",
            )
            st.dataframe(t.round(1), use_container_width=True)

    with st.expander("Rows of this day"):
        st.dataframe(cur.drop(columns=["PG"]), use_container_width=True, hide_index=True)
    with st.expander("Stored uploads"):
        for d, n, name in stored:
            r1, r2 = st.columns([4, 1])
            r1.write(f"{d:%a %Y-%m-%d} · {n} rows · {name}")
            if r2.button("Delete", key=f"del-{level}-{d}"):
                repo.delete_aging(level, d)
                st.rerun()


tab_res, tab_load, tab_ship = st.tabs(
    ["Appointment results", "Aging — load level", "Aging — shipment level"]
)
with tab_res:
    appt_rows = repo.list_appointments()
    if not appt_rows:
        st.info("No appointments yet — pick trucks on **Results** first.")
    else:
        appointment_results(appt_rows)

for tab, level in ((tab_load, "load"), (tab_ship, "shipment")):
    with tab:
        up = st.file_uploader(
            "Upload the Plan 7774 aging query result (load or shipment version, .xlsx / .csv)",
            type=["xlsx", "csv"],
            key=f"up-{level}",
            help="Run the query daily and upload it: each snapshot day is kept, so the trend "
            "builds up. A new upload of the same day replaces that day.",
        )
        if up is not None:
            key = f"done-{level}-{up.name}-{up.size}"
            if not st.session_state.get(key):
                try:
                    frame = read_upload(up.getvalue(), up.name)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    got = detect_level(frame)
                    day = snapshot_day(frame, hub_today())
                    repo.save_aging(got, day, to_records(frame), up.name)
                    st.session_state[key] = True
                    st.success(
                        f"Saved {len(frame)} {got}-level rows for {day:%Y-%m-%d}"
                        + ("" if got == level else f" (shown in the {got}-level tab)")
                        + "."
                    )
        aging_view(level)
