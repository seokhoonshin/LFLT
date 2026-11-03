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
    AGING_LEVEL,
    DAY_COLS,
    DAY_LABELS,
    PERF_LEVEL,
    RDD_COLS,
    RDD_LABELS,
    aging_frame,
    aging_table,
    dash,
    perf_by_period,
    perf_frame,
    read_aging_summary,
    read_perf,
    to_records,
)
from routeopt.appointments import OUTCOMES, outcome_summary, period_key, stop_outcomes
from routeopt.domain import hub_today

from _common import get_repo, osrm_status_line, setup_page, team_share

setup_page("Analyze")
osrm_status_line()
VIEWER = team_share()  # a viewer sees the team's data; uploads and deletes are hidden
repo = get_repo()
OUTCOME_COLOR = {"Confirmed": "#0ca30c", "Declined": "#d03b3b", "Open": "#9a9a92"}
OUTCOME_ICON = {"Confirmed": "✅", "Declined": "⛔", "Open": "⏳"}
# categorical slots in fixed order (Hold, Requested); buckets: one hue, light -> dark
STATUS_COLOR = {"Hold": "#2a78d6", "Requested": "#eb6834"}
PERF_BAR = "#76b5ae"  # the teal of the shared performance chart
PERF_TARGET = "#c0392b"
PERF_BASELINE = "#6b6b66"  # the past performance: neutral, dashed
BUCKET_BLUES = ["#cfe0f6", "#9cc1ec", "#6aa1e2", "#3b82d6", "#225fab", "#9a9a92"]

st.title("Analyze")
st.caption(
    "Dashboards: appointment results over time, LFLT performance (stops per load) and the "
    "LFLT aging of the open Plan 7774 loads."
)


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


# ---- LFLT aging (the aging summary query) -------------------------------------------------
def aging_view() -> None:
    """The two shared aging tables for a day, and how they moved over the days."""
    up = (
        None
        if VIEWER
        else st.file_uploader(
            "Upload the LFLT Aging summary query result (.xlsx / .csv)",
            type=["xlsx", "csv"],
            key="up-aging",
            help="One upload per day (its AS_OF day). A new upload of the same day replaces it.",
        )
    )
    if up is not None:
        key = f"done-aging-{up.name}-{up.size}"
        if not st.session_state.get(key):
            try:
                day, frame = read_aging_summary(up.getvalue(), up.name, hub_today())
            except ValueError as exc:
                st.error(str(exc))
            else:
                repo.save_aging(AGING_LEVEL, day, to_records(frame), up.name)
                st.session_state[key] = True
                st.success(f"Saved the LFLT aging of {day:%m/%d/%Y}.")
    df = aging_frame(repo.load_aging(AGING_LEVEL))
    if df.empty:
        st.info("No aging upload yet: run the LFLT Aging summary query and upload the result.")
        return
    days = sorted(df["DAY"].unique(), reverse=True)
    day = st.selectbox(
        "Day", days, format_func=lambda d: d.strftime("%a %m/%d/%Y"), key="aging-day"
    )
    cur = df[df["DAY"] == day]
    st.markdown(f"##### {day.month}/{day.day} LFLT Aging")
    for group in ("PG30,50,80", "PG20"):
        t = aging_table(cur, group)
        shown = t.copy()
        for c in shown.columns[2:]:
            shown[c] = [dash(v) for v in shown[c]]
        st.dataframe(shown, hide_index=True, use_container_width=True)

    st.markdown("##### Over the days")
    view = st.radio(
        "Show",
        ["PG30,50,80 by days in TMS", "PG20 by RDD", "Hold / Requested"],
        horizontal=True,
        key="aging-trend",
    )
    if view == "Hold / Requested":
        part = df[df["STATUS"].isin(["Hold", "Requested"])]
        long = part.groupby(["DAY", "STATUS"], as_index=False)["VOLUME"].sum()
        long = long.rename(columns={"STATUS": "Group"})
        domain, colors = ["Hold", "Requested"], [STATUS_COLOR["Hold"], STATUS_COLOR["Requested"]]
    else:
        group = "PG20" if view == "PG20 by RDD" else "PG30,50,80"
        cols = RDD_COLS if group == "PG20" else DAY_COLS
        labels = RDD_LABELS if group == "PG20" else DAY_LABELS
        part = df[(df["PLAN_GROUP"] == group) & df["STATUS"].isin(["Hold", "Requested"])]
        long = (
            part.groupby("DAY", as_index=False)[cols]
            .sum()
            .melt(id_vars="DAY", var_name="Group", value_name="VOLUME")
        )
        long["Group"] = long["Group"].map(labels)
        domain = [labels[c] for c in cols]
        colors = ["#d03b3b", "#9cc1ec"] if group == "PG20" else BUCKET_BLUES[: len(cols)]
    long["order"] = long["Group"].map({d: i for i, d in enumerate(domain)})
    chart = (
        alt.Chart(long)
        .mark_bar(size=18 if long["DAY"].nunique() < 30 else 8)
        .encode(
            x=alt.X(
                "yearmonthdate(DAY):O", title="Day", axis=alt.Axis(labelAngle=0, format="%m/%d")
            ),
            y=alt.Y("VOLUME:Q", title="Volume (cu ft)", axis=alt.Axis(grid=True)),
            color=alt.Color(
                "Group:N",
                title=None,
                scale=alt.Scale(domain=domain, range=colors),
                legend=alt.Legend(orient="top"),
            ),
            order=alt.Order("order:Q"),
            tooltip=[
                alt.Tooltip("yearmonthdate(DAY):T", title="Day", format="%m/%d/%Y"),
                alt.Tooltip("Group:N"),
                alt.Tooltip("VOLUME:Q", title="Volume", format=",.0f"),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)
    with st.expander("Stored uploads"):
        for d, n, name in repo.list_aging(AGING_LEVEL):
            r1, r2 = st.columns([4, 1])
            r1.write(f"{d:%a %m/%d/%Y} · {n} rows · {name}")
            if not VIEWER and r2.button("Delete", key=f"del-aging-{d}"):
                repo.delete_aging(AGING_LEVEL, d)
                st.rerun()


def perf_view() -> None:
    """LFLT performance: average stops per load by day / week / month / year vs target."""
    up = (
        None
        if VIEWER
        else st.file_uploader(
            "Upload the LFLT performance query result (load level, .xlsx / .csv)",
            type=["xlsx", "csv"],
            key="up-perf",
            help="Uploads add up: a load uploaded again is replaced by its latest values.",
        )
    )
    if up is not None:
        key = f"done-perf-{up.name}-{up.size}"
        if not st.session_state.get(key):
            try:
                frame = read_perf(up.getvalue(), up.name)
            except ValueError as exc:
                st.error(str(exc))
            else:
                repo.save_aging(PERF_LEVEL, hub_today(), to_records(frame), up.name)
                st.session_state[key] = True
                st.success(f"Saved {len(frame)} loads.")
    df = perf_frame(repo.load_aging(PERF_LEVEL))
    if df.empty:
        st.info("No LFLT performance upload yet: run the LFLT performance query and upload it.")
        return
    c1, c2, c3, c4, c5, c6 = st.columns([1.3, 1.2, 1.2, 0.7, 0.7, 0.7])
    period = c1.radio(
        "Group by", ["Day", "Week", "Month", "Year"], index=2, horizontal=True, key="perf-period"
    )
    first, last = min(df["DAY"]), max(df["DAY"])
    span = c2.date_input("Days", (first, last), min_value=first, max_value=last, key="perf-span")
    lo, hi = (span[0], span[-1]) if isinstance(span, tuple) and span else (first, last)
    origins = sorted(set(df["ORIGIN"]) | {"LGC2", "LGC7", "LGC8"})
    pick = c3.multiselect("Origin", origins, default=origins, key="perf-org")
    target = c4.number_input("Target stops", 1.0, 20.0, 3.0, 0.5, key="perf-target")
    baseline = c5.number_input(
        "Baseline stops",
        0.1,
        20.0,
        1.71,
        0.01,
        format="%.2f",
        key="perf-baseline",
        help="Stops per load before LFLT (the past performance), to compare against.",
    )
    c6.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
    show_baseline = c6.toggle(
        "Baseline", value=True, key="perf-baseline-on", help="Show the baseline line on the chart."
    )
    cur = df[(df["DAY"] >= lo) & (df["DAY"] <= hi) & df["ORIGIN"].isin(pick)]
    if cur.empty:
        st.warning("No loads in this range.")
        return
    k = st.columns(5)
    k[0].metric("Loads", f"{cur['LOAD_ID'].nunique():,}")
    k[1].metric(
        "Avg stops per load",
        f"{cur['STOPS'].mean():.2f}",
        f"{cur['STOPS'].mean() - target:+.2f} vs target",
    )
    k[2].metric(f"Loads at {target:g}+ stops", f"{100 * (cur['STOPS'] >= target).mean():.0f}%")
    if "VOLUME" in cur:
        k[3].metric("Avg volume per load", f"{cur['VOLUME'].mean():,.0f} cu ft")
    lift = (cur["STOPS"].mean() - baseline) / baseline
    k[4].metric(
        f"vs baseline {baseline:g} stops",
        f"{lift:+.0%}",
        f"{cur['STOPS'].mean() - baseline:+.2f} stops per load",
        help="How much the average stops per load is above (or below) the baseline.",
    )
    by = perf_by_period(cur, period, target)
    order = list(by["PERIOD"])
    top = max(float(by["AVG_STOPS"].max()), target, baseline) * 1.2
    bars = (
        alt.Chart(by)
        .mark_bar(
            color=PERF_BAR,
            size=40 if len(order) < 15 else 14,
            cornerRadiusTopLeft=3,
            cornerRadiusTopRight=3,
        )
        .encode(
            x=alt.X("PERIOD:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y(
                "AVG_STOPS:Q",
                title="Avg stops per load",
                scale=alt.Scale(domain=[0, top]),
                axis=alt.Axis(grid=True),
            ),
            tooltip=[
                alt.Tooltip("PERIOD:N", title=period),
                alt.Tooltip("AVG_STOPS:Q", title="Avg stops / load", format=".2f"),
                alt.Tooltip("LOADS:Q", title="Loads"),
                alt.Tooltip("STOPS:Q", title="Stops"),
                alt.Tooltip("AT_TARGET:Q", title=f"Loads at {target:g}+ stops", format=".0%"),
                alt.Tooltip("AVG_VOLUME:Q", title="Avg cu ft / load", format=",.0f"),
            ],
        )
    )
    labels = bars.mark_text(dy=-8, color="#555", fontSize=11).encode(
        text=alt.Text("AVG_STOPS:Q", format=".1f")
    )
    line = (
        alt.Chart(pd.DataFrame({"y": [target]}))
        .mark_rule(color=PERF_TARGET, strokeWidth=2)
        .encode(y="y:Q")
    )
    tag = (
        alt.Chart(pd.DataFrame({"y": [target], "t": [f"TARGET ({target:g} stops)"]}))
        .mark_text(align="left", dx=4, dy=-7, color=PERF_TARGET, fontSize=11)
        .encode(y="y:Q", x=alt.value(0), text="t:N")
    )
    layers = bars + labels + line + tag
    if show_baseline:
        base_line = (
            alt.Chart(pd.DataFrame({"y": [baseline]}))
            .mark_rule(color=PERF_BASELINE, strokeWidth=2, strokeDash=[6, 4])
            .encode(y="y:Q")
        )
        base_tag = (
            alt.Chart(pd.DataFrame({"y": [baseline], "t": [f"BASELINE ({baseline:g} stops)"]}))
            .mark_text(align="left", dx=4, dy=-7, color=PERF_BASELINE, fontSize=11)
            .encode(y="y:Q", x=alt.value(0), text="t:N")
        )
        layers = layers + base_line + base_tag
    st.markdown(f"##### LFLT — average stops per load by {period.lower()}")
    st.altair_chart(layers.properties(height=360), use_container_width=True)
    with st.expander("Table view"):
        show = by.drop(columns=["START"]).rename(
            columns={
                "PERIOD": period,
                "LOADS": "Loads",
                "AVG_STOPS": "Avg stops / load",
                "STOPS": "Stops",
                "AT_TARGET": f"Loads at {target:g}+ stops",
                "AVG_VOLUME": "Avg cu ft / load",
            }
        )
        st.dataframe(
            show.style.format(
                {f"Loads at {target:g}+ stops": "{:.0%}", "Avg cu ft / load": "{:,.0f}"}
            ),
            hide_index=True,
            use_container_width=True,
        )
    with st.expander("Loads"):
        st.dataframe(cur.drop(columns=["SNAPSHOT_DATE"]), hide_index=True, use_container_width=True)
    with st.expander("Stored uploads"):
        for d, n, name in repo.list_aging(PERF_LEVEL):
            r1, r2 = st.columns([4, 1])
            r1.write(f"{d:%a %Y-%m-%d} · {n} loads · {name}")
            if not VIEWER and r2.button("Delete", key=f"del-perf-{d}"):
                repo.delete_aging(PERF_LEVEL, d)
                st.rerun()


tab_res, tab_perf, tab_aging = st.tabs(["Appointment results", "LFLT performance", "LFLT Aging"])
with tab_perf:
    perf_view()
with tab_aging:
    aging_view()
with tab_res:
    appt_rows = repo.list_appointments()
    if not appt_rows:
        st.info("No appointments yet — pick trucks on **Results** first.")
    else:
        appointment_results(appt_rows)
