"""New Run - upload, validate, choose settings, optimize (brief 9.2)."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import datetime as dt
import hashlib
import io
import time
from typing import Any

import pandas as pd
import streamlit as st
from routeopt.config import (
    SETTING_TOOLTIPS,
    RunSettings,
    preset_description,
    validate_penalty_invariants,
)
from routeopt.domain import HUB_TIMEZONE, hub_today
from routeopt.ingest.census_geocoder import CensusGeocoder
from routeopt.ingest.excel_loader import CANONICAL_COLUMNS, REQUIRED_COLUMNS
from routeopt.pipeline import OptimizeJob, build_matrix, ingest, select_trucks
from routeopt.travel import select_provider

from _common import (
    fallback_banner,
    get_config,
    get_repo,
    osrm_status_line,
    set_current_run,
    setup_page,
    status_text,
    today_plus,
)

setup_page("New Run")
osrm_status_line()
fallback_banner()
st.title("New Run")

repo = get_repo()
cfg = get_config()
TIP = SETTING_TOOLTIPS

# ---------------------------------------------------------------------------------------
# 1. Upload
# ---------------------------------------------------------------------------------------
st.subheader("1 · Upload the shipment workbook")
uploaded = st.file_uploader("Excel (.xlsx) or CSV with one row per shipment", type=["xlsx", "csv"])
if uploaded is not None:
    data = uploaded.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    if st.session_state.get("upload_digest") != digest:
        st.session_state.update(
            {
                "upload_bytes": data,
                "upload_name": uploaded.name,
                "upload_digest": digest,
                "mapping": {},
                "exclude_rows": set(),
            }
        )
        st.session_state.pop("ingest_cache", None)
if "upload_bytes" not in st.session_state:
    st.info(
        "Upload a workbook to start. Need a file? `routeopt sample --all-coords` writes a synthetic one."
    )
    st.stop()
st.caption(f"File: **{st.session_state['upload_name']}**")

# ---------------------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------------------
st.subheader("2 · Settings")
defaults: RunSettings = (
    st.session_state.get("settings_defaults") or repo.load_last_settings() or RunSettings()
)
if "settings_defaults" not in st.session_state:
    st.session_state["settings_defaults"] = defaults
trucks_all = repo.list_trucks()
active_numbers = [t.truck_number for t in trucks_all if t.active]

top1, top2, top3 = st.columns([1.2, 1, 1])
preset = top1.radio(
    "Preset",
    ["A", "B", "Custom"],
    index=["A", "B", "Custom"].index(defaults.preset),
    format_func=lambda p: {
        "A": "A — Fewest trucks (packed)",
        "B": "B — Balanced",
        "Custom": "Custom",
    }[p],
    help=TIP["preset"],
    horizontal=True,
)
st.caption(preset_description(preset))  # type: ignore[arg-type]
plan_date = top2.date_input("Delivery day (D)", value=today_plus(2), help=TIP["plan_date"])
top2.caption(
    f"Run day (D-2): **{(plan_date - dt.timedelta(days=2)).isoformat()}** · today at the hub ({HUB_TIMEZONE}): {hub_today().isoformat()}"
)
solve_time = top3.slider(
    "Solve time (seconds)",
    10,
    60,
    max(10, defaults.solve_time_seconds),
    help=TIP["solve_time_seconds"],
)
pull_forward = st.multiselect(
    "Pull-forward pools",
    [1, 2],
    default=[d for d in defaults.pull_forward_days if d in (1, 2)],
    format_func=lambda d: f"D+{d}",
    help=TIP["pull_forward_days"],
)
trucks_selected = st.multiselect(
    "Trucks available",
    active_numbers,
    default=[t for t in (defaults.trucks_selected or active_numbers) if t in active_numbers]
    or active_numbers,
    help=TIP["trucks_selected"],
)

with st.expander("Advanced settings", expanded=False):
    a1, a2, a3, a4 = st.columns(4)
    shift_start = a1.time_input("Shift start", value=defaults.shift_start, help=TIP["shift_start"])
    on_duty_hours = a2.number_input(
        "On-duty hours", 1.0, 16.0, float(defaults.on_duty_hours), 0.5, help=TIP["on_duty_hours"]
    )
    truck_volume = a3.number_input(
        "Truck volume (cu ft)",
        50.0,
        5000.0,
        float(defaults.truck_volume_cuft),
        10.0,
        help=TIP["truck_volume_cuft"],
    )
    max_loads = a4.number_input(
        "Max loads per truck",
        1,
        5,
        int(defaults.max_loads_per_truck),
        help=TIP["max_loads_per_truck"],
    )
    b1, b2, b3, b4 = st.columns(4)
    stop_buffer = b1.number_input(
        "Stop buffer (min)",
        0,
        120,
        int(defaults.stop_buffer_minutes),
        help=TIP["stop_buffer_minutes"],
    )
    reload_minutes = b2.number_input(
        "Reload (min)", 0, 240, int(defaults.reload_minutes), help=TIP["reload_minutes"]
    )
    lunch_minutes = b3.number_input(
        "Lunch (min)", 0, 120, int(defaults.lunch_minutes), help=TIP["lunch_minutes"]
    )
    eta_window = b4.number_input(
        "ETA window (± hours)",
        0.5,
        6.0,
        float(defaults.eta_window_hours),
        0.5,
        help=TIP["eta_window_hours"],
    )
    c1, c2, c3, c4 = st.columns(4)
    any_time = c1.checkbox(
        "Lunch any time in the shift",
        value=defaults.lunch_earliest is None and defaults.lunch_latest is None,
        help=TIP["lunch_window"],
    )
    lunch_earliest = c2.time_input(
        "Lunch earliest start",
        value=defaults.lunch_earliest or dt.time(11, 0),
        disabled=any_time,
        help=TIP["lunch_window"],
    )
    lunch_latest = c3.time_input(
        "Lunch latest start",
        value=defaults.lunch_latest or dt.time(14, 0),
        disabled=any_time,
        help=TIP["lunch_window"],
    )
    lunch_mode = c4.selectbox(
        "Lunch mode",
        ["smart", "simple"],
        index=0 if defaults.lunch_mode == "smart" else 1,
        help=TIP["lunch_mode"],
    )
    d1, d2, d3, d4 = st.columns(4)
    use_weight_cap = d1.checkbox(
        "Cap weight",
        value=defaults.truck_weight_lb_cap is not None,
        help=TIP["truck_weight_lb_cap"],
    )
    weight_cap = d2.number_input(
        "Weight cap (lb)",
        100.0,
        100000.0,
        float(defaults.truck_weight_lb_cap or 10000.0),
        100.0,
        disabled=not use_weight_cap,
    )
    use_pieces_cap = d3.checkbox(
        "Cap pieces", value=defaults.truck_pieces_cap is not None, help=TIP["truck_pieces_cap"]
    )
    pieces_cap = d4.number_input(
        "Pieces cap", 1, 1000, int(defaults.truck_pieces_cap or 40), disabled=not use_pieces_cap
    )
    e1, e2, e3, e4 = st.columns(4)
    travel_provider = e1.selectbox(
        "Travel times",
        ["auto", "osrm", "haversine"],
        index=["auto", "osrm", "haversine"].index(defaults.travel_provider),
        format_func=lambda v: {
            "auto": "Auto (OSRM if available)",
            "osrm": "OSRM only",
            "haversine": "Straight-line estimate",
        }[v],
        help=TIP["travel_provider"],
    )
    road_factor = e2.number_input(
        "Straight-line road factor",
        1.0,
        2.5,
        float(defaults.haversine_road_factor),
        0.05,
        help=TIP["haversine_road_factor"],
    )
    avg_speed = e3.number_input(
        "Straight-line speed (mph)",
        5.0,
        70.0,
        float(defaults.haversine_avg_speed_mph),
        1.0,
        help=TIP["haversine_avg_speed_mph"],
    )
    first_solution = e4.selectbox(
        "First solution strategy",
        ["PARALLEL_CHEAPEST_INSERTION", "PATH_CHEAPEST_ARC"],
        index=0 if defaults.first_solution_strategy == "PARALLEL_CHEAPEST_INSERTION" else 1,
        help=TIP["first_solution_strategy"],
    )
    f1, f2, f3, f4 = st.columns(4)
    pf1 = f1.number_input(
        "D+1 stop value (min of driving)",
        1,
        330,
        int(defaults.pull_forward_value_minutes.get(1, 240)),
        help=TIP["pull_forward_value_minutes"],
    )
    pf2 = f2.number_input(
        "D+2 stop value (min of driving)",
        1,
        330,
        int(defaults.pull_forward_value_minutes.get(2, 120)),
        help=TIP["pull_forward_value_minutes"],
    )
    fixed_cost = f3.number_input(
        "Custom: fixed cost per truck",
        0,
        5_000_000,
        int(defaults.truck_fixed_cost or 20000),
        1000,
        disabled=preset != "Custom",
        help=TIP["truck_fixed_cost"],
    )
    span_cost = f4.number_input(
        "Custom: span cost coefficient",
        0,
        1000,
        int(defaults.span_cost_coefficient or 0),
        disabled=preset != "Custom",
        help=TIP["span_cost_coefficient"],
    )

try:
    settings = RunSettings(
        plan_date=plan_date,
        pull_forward_days=[int(d) for d in pull_forward],
        trucks_selected=list(trucks_selected),
        shift_start=shift_start,
        on_duty_hours=float(on_duty_hours),
        truck_volume_cuft=float(truck_volume),
        truck_weight_lb_cap=float(weight_cap) if use_weight_cap else None,
        truck_pieces_cap=int(pieces_cap) if use_pieces_cap else None,
        stop_buffer_minutes=int(stop_buffer),
        reload_minutes=int(reload_minutes),
        lunch_minutes=int(lunch_minutes),
        lunch_earliest=None if any_time else lunch_earliest,
        lunch_latest=None if any_time else lunch_latest,
        lunch_mode=lunch_mode,  # type: ignore[arg-type]
        max_loads_per_truck=int(max_loads),
        preset=preset,  # type: ignore[arg-type]
        solve_time_seconds=int(solve_time),
        eta_window_hours=float(eta_window),
        travel_provider=travel_provider,  # type: ignore[arg-type]
        haversine_road_factor=float(road_factor),
        haversine_avg_speed_mph=float(avg_speed),
        pull_forward_value_minutes={1: int(pf1), 2: int(pf2)},
        truck_fixed_cost=int(fixed_cost) if preset == "Custom" else None,
        span_cost_coefficient=int(span_cost) if preset == "Custom" else None,
        first_solution_strategy=first_solution,  # type: ignore[arg-type]
    )
except ValueError as exc:
    st.error(f"Settings problem: {exc}")
    st.stop()
problems = validate_penalty_invariants(settings, max(1, len(trucks_selected)))
for p in problems:
    st.error(p)

# ---------------------------------------------------------------------------------------
# 3. Validation report
# ---------------------------------------------------------------------------------------
st.subheader("3 · Validation report")
mapping: dict[str, str] = st.session_state.get("mapping", {})
exclude_rows: set[int] = st.session_state.get("exclude_rows", set())
ingest_key = (
    st.session_state["upload_digest"],
    plan_date.isoformat(),
    tuple(sorted(mapping.items())),
    tuple(sorted(exclude_rows)),
    settings.stop_buffer_minutes,
    settings.coverage_radius_miles,
    settings.truck_volume_cuft,
)
cache = st.session_state.get("ingest_cache")
if cache is None or cache[0] != ingest_key:
    with st.spinner("Checking the workbook (and geocoding any rows without coordinates)…"):
        geocoder = CensusGeocoder(cache=repo.geocode_cache())
        result = ingest(
            io.BytesIO(st.session_state["upload_bytes"]),
            settings,
            geocoder=geocoder,
            mapping=mapping or None,
            exclude_rows=exclude_rows or None,
        )
    st.session_state["ingest_cache"] = (ingest_key, result)
else:
    result = cache[1]

if result.mapping_missing:
    st.warning(
        "Some required columns were not recognised. Match them below and press **Apply mapping**."
    )
    headers = ["", *list(result.headers)]
    chosen: dict[str, str] = {}
    cols = st.columns(3)
    for i, canonical in enumerate(CANONICAL_COLUMNS):
        default = mapping.get(canonical, "")
        required = canonical in REQUIRED_COLUMNS
        label = f"{canonical}{' *' if required else ''}"
        sel = cols[i % 3].selectbox(
            label,
            headers,
            index=headers.index(default) if default in headers else 0,
            key=f"map_{canonical}",
        )
        if sel:
            chosen[canonical] = sel
    if st.button("Apply mapping", type="primary"):
        st.session_state["mapping"] = chosen
        st.session_state.pop("ingest_cache", None)
        st.rerun()
    st.stop()

val = result.validation
cons = result.consolidation
m1, m2, m3, m4 = st.columns(4)
m1.metric("Rows read", val.rows_read)
m2.metric("Errors", len(val.errors))
m3.metric("Warnings", len(val.warnings))
m4.metric("Geocoded rows", len(val.geocoded_rows))
if val.errors:
    st.error(f"{len(val.errors)} problem(s) block the run. Fix the workbook and upload it again.")
    st.dataframe(pd.DataFrame(val.issues_table("error")), hide_index=True, use_container_width=True)
    if val.geocode_failures:
        failing = sorted({e.row for e in val.geocode_failures})
        if st.button(f"Exclude the {len(failing)} row(s) that could not be geocoded and continue"):
            st.session_state["exclude_rows"] = exclude_rows | set(failing)
            st.session_state.pop("ingest_cache", None)
            st.rerun()
if val.warnings:
    with st.expander(
        f"{len(val.warnings)} warning(s) — the run can proceed", expanded=not val.errors
    ):
        st.dataframe(
            pd.DataFrame(val.issues_table("warning")), hide_index=True, use_container_width=True
        )
if val.geocoded_rows:
    st.info(
        f"Filled coordinates for rows {', '.join(str(r) for r in val.geocoded_rows[:20])}{'…' if len(val.geocoded_rows) > 20 else ''} using the Census geocoder."
    )
if cons is not None:
    by_day = ", ".join(f"{d}: {n}" for d, n in sorted(cons.counts_by_day.items()))
    by_tier = ", ".join(f"tier {t}: {n}" for t, n in cons.counts_by_tier.items())
    st.success(f"**{cons.summary}** — by day {by_day} — {by_tier}")
    in_scope = [
        s
        for s in cons.stops
        if s.day_tag.offset == 0 or s.day_tag.offset in settings.pull_forward_days
    ]
    demand_vol = sum(s.delivery_volume for s in in_scope)
    demand_min = sum(s.service_total_minutes for s in in_scope)
    n_trucks = max(1, len(trucks_selected))
    st.caption(
        f"In scope: {len(in_scope)} stops · delivery volume {demand_vol:,.0f} cu ft vs "
        f"{n_trucks * settings.truck_volume_cuft:,.0f} cu ft across {n_trucks} trucks × 1 load · service "
        f"{demand_min:,.0f} min vs {n_trucks * (settings.on_duty_hours * 60 - settings.lunch_minutes):,.0f} min available."
    )

# ---------------------------------------------------------------------------------------
# 4. Optimize
# ---------------------------------------------------------------------------------------
st.subheader("4 · Optimize")
job: OptimizeJob | None = st.session_state.get("job")
if job is None:
    can_run = val.ok and cons is not None and bool(trucks_selected) and not problems
    if st.button("Optimize", type="primary", disabled=not can_run):
        assert cons is not None
        trucks = select_trucks(settings, trucks_all)
        with st.spinner("Fetching travel times…"):
            choice = select_provider(settings, cfg, cache=repo.travel_cache())
            matrix = build_matrix(cons.stops, choice)
        st.session_state["travel_fallback"] = choice.fallback_used
        if choice.fallback_used:
            st.warning(f"{choice.reason} Using straight-line estimates for this run.")
        repo.save_last_settings(settings)
        st.session_state["settings_defaults"] = settings
        job = OptimizeJob(cons.stops, matrix, settings, trucks)
        st.session_state["job"] = job
        st.session_state["job_meta"] = {
            "settings": settings,
            "stops": cons.stops,
            "matrix": matrix,
            "trucks": trucks,
            "shipments": val.shipments,
            "warnings": [w.message for w in val.warnings],
        }
        st.rerun()
    st.stop()

meta: dict[str, Any] = st.session_state["job_meta"]
job.poll()
if not job.done:
    limit = meta["settings"].solve_time_seconds
    elapsed = min(job.elapsed, limit + 5)
    st.progress(
        min(0.99, elapsed / max(1, limit)),
        text=f"Optimizing… {elapsed:.0f} s of up to {limit} s (plus validation)",
    )
    if st.button("Cancel"):
        job.cancel()
        st.session_state.pop("job", None)
        st.session_state.pop("job_meta", None)
        st.warning("Run cancelled.")
        st.stop()
    time.sleep(0.7)
    st.rerun()

if job.error or job.result is None:
    st.error(f"The optimizer could not finish: {job.error or 'no result'}")
    st.session_state.pop("job", None)
    st.stop()

res = job.result
plan = res.plan
if not plan.solver_stats.first_solution_found:
    st.error(
        f"No plan could be built: {plan.solver_stats.message or status_text(plan.solver_stats.status)}"
    )
    st.session_state.pop("job", None)
    st.stop()

repo.save_run(
    plan,
    res.all_stops,
    meta["settings"],
    res.full_matrix or res.matrix,
    meta["shipments"],
    meta["trucks"],
    source_name=st.session_state.get("upload_name", ""),
    source_bytes=st.session_state.get("upload_bytes"),
    warnings=meta["warnings"],
    in_scope_ids=res.in_scope_ids,
)
set_current_run(plan.run_id, 0)
st.session_state.pop("job", None)
st.session_state.pop("job_meta", None)
st.success(
    f"Plan ready: {status_text(plan.solver_stats.status)} in {plan.solver_stats.wall_time_s} s. Saved as run {plan.run_id}."
)
st.switch_page("pages/3_Results.py")
