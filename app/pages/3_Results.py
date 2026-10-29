"""Results - KPIs, map, per-truck loads, unassigned panel, exports (brief 9.3)."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from routeopt.appointments import appointment_rows
from routeopt.domain import SITE_BY_CODE, Plan, Stop, clock
from routeopt.edit.revalidate import can_export
from routeopt.export.excel import export_excel
from routeopt.export.load_summary import export_load_summary_pdf
from routeopt.export.mapping import build_map, map_html
from routeopt.export.route_sheets_pdf import export_route_sheets
from routeopt.travel.osrm import OSRMProvider
from routeopt.travel.tomtom import is_road_source, source_label

from _common import (
    current_run_id,
    current_version,
    fallback_banner,
    get_config,
    get_repo,
    kpi_cards,
    load_record,
    load_table,
    minutes_text,
    osrm_healthy,
    osrm_status_line,
    set_current_run,
    setup_page,
    status_text,
    tier_label,
)

setup_page("Results")
osrm_status_line()
repo = get_repo()
cfg = get_config()

runs = repo.list_runs(limit=200)
if not runs:
    st.info("No runs yet — start one on the **New Run** page.")
    st.stop()
run_ids = [r.run_id for r in runs]
default_run = current_run_id() if current_run_id() in run_ids else run_ids[0]
sel_run = st.sidebar.selectbox(
    "Run",
    run_ids,
    index=run_ids.index(default_run),
    format_func=lambda r: f"{r} · D={next(x.plan_date for x in runs if x.run_id == r)}",
)
rec = load_record(sel_run)
versions = sorted(rec.plans)
default_version = (
    current_version()
    if sel_run == current_run_id() and current_version() in versions
    else versions[-1]
)
sel_version = st.sidebar.selectbox(
    "Version",
    versions,
    index=versions.index(default_version),
    format_func=lambda v: "v0 · optimizer" if v == 0 else f"v{v} · planner edit",
)
set_current_run(sel_run, sel_version)
plan: Plan = rec.plans[sel_version]
stops: list[Stop] = rec.stops
by_id = {s.stop_id: s for s in stops}
settings = rec.settings

st.title(f"Results — run {plan.run_id} · v{plan.plan_version}")
st.caption(
    f"Delivery day **{plan.plan_date.isoformat()}** · preset **{plan.settings.get('preset')}** · "
    f"optimizer {status_text(plan.solver_stats.status)} in {plan.solver_stats.wall_time_s} s · "
    f"travel times: **{source_label(plan.matrix_source)}** · "
    f"source file: {rec.source_name or '—'}"
)
fallback_banner(plan)
for w in plan.warnings:
    if "ESTIMATED" not in w:
        st.warning(w)
hard = plan.hard_violations
soft = [v for v in plan.violations if v.severity == "warning"]
if hard:
    st.error(
        f"This version has {len(hard)} hard violation(s) — see the Edit page. "
        + "; ".join(str(v) for v in hard[:3])
    )
if soft:
    with st.expander(f"{len(soft)} validator note(s)"):
        for v in soft:
            st.write(f"• {v}")

kpi_cards(plan)

# ---- map --------------------------------------------------------------------------------
st.subheader("Map")
geometry_fn = None
if is_road_source(plan.matrix_source) and osrm_healthy():
    provider = OSRMProvider(cfg.osrm_url, timeout_s=cfg.osrm_timeout_s)
    geom_cache: dict = st.session_state.setdefault("geometry_cache", {})

    def geometry_fn(coords):  # type: ignore[no-untyped-def]
        key = tuple(coords)
        if key not in geom_cache:
            geom_cache[key] = provider.route_geometry(list(coords))
        return geom_cache[key]


# Embedded as plain HTML (fixed height) so the layout is stable even when the map's
# JavaScript is slow to load; set ROUTEOPT_MAP_COMPONENT=st_folium to use streamlit-folium.
if cfg.map_component == "st_folium":
    from streamlit_folium import st_folium

    st_folium(
        build_map(plan, stops, settings, geometry_fn=geometry_fn),
        height=580,
        use_container_width=True,
        returned_objects=[],
    )
else:
    components.html(
        map_html(plan, stops, settings, geometry_fn=geometry_fn), height=580, scrolling=False
    )
if not is_road_source(plan.matrix_source):
    st.caption("Straight lines: OSRM was not available for this run, so no road geometry is drawn.")

# ---- one map per load (the picked trucks' loads by default) ------------------------------
all_loads = [(t.number, ld.seq) for t in plan.trucks for ld in t.loads if ld.stops]
picked_now = repo.selected_trucks(plan.run_id)
default_loads = [x for x in all_loads if x[0] in picked_now] or all_loads[:2]
chosen = st.multiselect(
    "Maps by load",
    all_loads,
    default=default_loads,
    format_func=lambda x: f"{x[0]} · Load {x[1]}",
    help="One map per load, two per row. By default the loads of the trucks picked with "
    "Select this truck (else the first two).",
)
for i in range(0, len(chosen), 2):
    cols = st.columns(2)
    for col, (number, seq) in zip(cols, chosen[i : i + 2], strict=False):
        with col:
            st.markdown(f"**{number} · Load {seq}**")
            components.html(
                map_html(plan, stops, settings, geometry_fn=geometry_fn, only={(number, seq)}),
                height=420,
                scrolling=False,
            )

# ---- trucks ------------------------------------------------------------------------------
st.subheader("Trucks and loads")
st.caption(
    "Pick the trucks you will actually run with **Select this truck**: their stops go to the "
    "**Appointments** page (sidebar), where you track each appointment request."
)
picked = repo.selected_trucks(plan.run_id)
for truck in plan.trucks:
    loads = [load for load in truck.loads if load.stops]
    if not loads:
        continue
    title = (
        f"{truck.number} · {truck.driver_name or 'no driver'} — {len(loads)} load(s), "
        f"{sum(len(load.stops) for load in loads)} stops, on duty {minutes_text(truck.on_duty_minutes)} "
        f"(start {clock(truck.start_s if truck.start_s is not None else loads[0].depart_s)}"
        f" → back {clock(loads[-1].return_s)}), lunch {truck.lunch.where}"
    )
    head, pick = st.columns([8, 1.4])
    if truck.number in picked:
        pick.button(
            "✓ Selected",
            key=f"picked-{truck.number}",
            disabled=True,
            use_container_width=True,
            help="Listed on the Appointments page (remove it there).",
        )
    elif pick.button(
        "Select this truck", key=f"pick-{truck.number}", type="primary", use_container_width=True
    ):
        added = repo.select_truck_for_appointments(appointment_rows(plan, stops, truck.number))
        st.toast(f"{truck.number}: {added} shipment(s) added to Appointments.")
        st.rerun()
    with head.expander(title):
        for load in loads:
            cap = settings.load_cap_cuft(load.pallet)
            st.markdown(
                f"**Load {load.seq}** from {SITE_BY_CODE[load.site].name}"
                + (" (pallet trip)" if load.pallet and cap != settings.truck_volume_cuft else "")
                + f" · depart {clock(load.depart_s)} · return {clock(load.return_s)} · "
                f"load-out {load.load_out_volume:.0f} cu ft ({100 * load.load_out_volume / cap:.0f}%) · "
                f"{load.one_way_miles:.0f} mi one way · "
                f"max on road {load.max_volume_on_road:.0f} cu ft · {load.drive_minutes:.0f} min / {load.distance_miles:.1f} mi driving"
                + (
                    f" · reload from {clock(load.reload_start_s)}"
                    if load.reload_start_s is not None
                    else ""
                )
                + (
                    f" · {load.deadhead_minutes:.0f} min DC-to-DC first"
                    if load.deadhead_minutes
                    else ""
                )
            )
            st.dataframe(
                load_table(plan, truck.number, load.seq, by_id),
                hide_index=True,
                use_container_width=True,
            )

# ---- unassigned panel ---------------------------------------------------------------------
st.subheader("Unassigned stops")
k = plan.kpis
n_trucks = k.trucks_selected or 1
st.caption(
    f"Capacity vs demand (D stops): delivery volume {k.demand_delivery_volume:,.0f} cu ft vs "
    f"{k.available_volume:,.0f} cu ft available across {n_trucks} trucks × 1 load; service time "
    f"{k.demand_service_minutes:,.0f} min vs {k.available_minutes:,.0f} min available "
    f"({n_trucks} trucks × ({settings.on_duty_hours:g} h − {settings.lunch_minutes} min lunch)). "
    "Extra loads add volume at the cost of reload time."
)
if not plan.unassigned:
    st.success("Every stop in scope is on a route.")
else:
    groups: dict[int, list] = defaultdict(list)
    for u in plan.unassigned:
        groups[u.tier].append(u)
    for tier in sorted(groups):
        items = groups[tier]
        vol = sum(u.volume for u in items)
        mins = sum(u.service_minutes for u in items)
        with st.expander(
            f"{tier_label(tier)} — {len(items)} stop(s), {vol:,.0f} cu ft, {mins:,.0f} service min",
            expanded=tier <= 3,
        ):
            rows = []
            for u in items:
                s = by_id.get(u.stop_id)
                rows.append(
                    {
                        "Stop": u.stop_id,
                        "Customer": s.customer_label if s else "",
                        "Address": f"{s.address}, {s.city} {s.zip}" if s else "",
                        "Shipments": len(s.shipments) if s else 0,
                        "Volume (cu ft)": u.volume,
                        "Service min": u.service_minutes,
                        "Reason": u.reason,
                        "Detail": u.detail,
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
if plan.out_of_scope:
    st.caption(
        f"{len(plan.out_of_scope)} stop(s) are not in scope because their pull-forward day is switched off."
    )

# ---- exports -------------------------------------------------------------------------------
st.subheader("Export")
gate = can_export(plan)
if not gate.allowed:
    st.error(gate.message + " Use the Edit page to fix the plan or to export anyway with a reason.")
e1, e2, e4, e3, _ = st.columns([1, 1, 1, 1, 2])
out_dir = Path(cfg.exports_dir)
if e1.button("Export Excel", disabled=not gate.allowed):
    path = export_excel(
        plan,
        stops,
        settings,
        out_dir,
        trucks_snapshot=rec.trucks,
        extra_warnings=rec.warnings,
        source_name=rec.source_name,
    )
    repo.record_export(
        plan.run_id, plan.plan_version, "xlsx", str(path), plan.export_override_reason
    )
    st.session_state["last_xlsx"] = str(path)
if e2.button("Export PDF route sheets", disabled=not gate.allowed):
    path = export_route_sheets(plan, stops, settings, out_dir, trucks_snapshot=rec.trucks)
    repo.record_export(
        plan.run_id, plan.plan_version, "pdf", str(path), plan.export_override_reason
    )
    st.session_state["last_pdf"] = str(path)
if e4.button("Export load summary PDF", disabled=not gate.allowed):
    path = export_load_summary_pdf(plan, stops, settings, out_dir)
    repo.record_export(
        plan.run_id, plan.plan_version, "pdf", str(path), plan.export_override_reason
    )
    st.session_state["last_summary_pdf"] = str(path)
if e3.button("Edit this plan", type="primary"):
    st.switch_page("pages/4_Edit.py")
for key, label, mime in (
    (
        "last_xlsx",
        "Download Excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ("last_pdf", "Download PDF", "application/pdf"),
    ("last_summary_pdf", "Download load summary PDF", "application/pdf"),
):
    p = st.session_state.get(key)
    if p and Path(p).exists() and plan.run_id in p and f"_v{plan.plan_version}" in p:
        st.download_button(
            label, data=Path(p).read_bytes(), file_name=Path(p).name, mime=mime, key=f"dl_{key}"
        )
        st.caption(f"Saved to {p}")
