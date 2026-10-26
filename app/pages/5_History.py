"""History - every run and version; open, re-export, compare, soft-delete (brief 9.5)."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pathlib import Path

import pandas as pd
import streamlit as st
from routeopt.edit.revalidate import can_export, diff_plans
from routeopt.export.excel import export_excel
from routeopt.export.route_sheets_pdf import export_route_sheets

from _common import (
    fallback_banner,
    forget_record,
    get_config,
    get_repo,
    load_record,
    minutes_text,
    osrm_status_line,
    set_current_run,
    setup_page,
    status_text,
)

setup_page("History")
osrm_status_line()
fallback_banner()
repo = get_repo()
cfg = get_config()

st.title("History")
show_deleted = st.checkbox("Show deleted runs", value=False)
runs = repo.list_runs(limit=500, include_deleted=show_deleted)
if not runs:
    st.info("No runs yet.")
    st.stop()

st.dataframe(
    pd.DataFrame(
        [
            {
                "Run": r.run_id,
                "Created": r.created_at.strftime("%Y-%m-%d %H:%M"),
                "Delivery day": r.plan_date.isoformat(),
                "Run day": r.run_date.isoformat(),
                "Preset": r.preset,
                "Status": status_text(r.status),
                "Trucks": f"{r.trucks_used}/{r.trucks_selected}",
                "Stops": f"{r.stops_served}/{r.stops_total}",
                "Unassigned": r.unassigned,
                "Versions": r.version_count,
                "Travel": r.matrix_source,
                "File": r.source_name,
                "Deleted": "yes" if r.deleted else "",
            }
            for r in runs
        ]
    ),
    hide_index=True,
    use_container_width=True,
)

run_ids = [r.run_id for r in runs]
st.subheader("Open a run")
sel = st.selectbox("Run", run_ids)
rec = load_record(sel)
versions = sorted(rec.plans)
ver = st.selectbox(
    "Version",
    versions,
    index=len(versions) - 1,
    format_func=lambda v: "v0 · optimizer" if v == 0 else f"v{v} · planner edit",
)
plan = rec.plans[ver]
k = plan.kpis
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trucks", f"{k.trucks_used}/{k.trucks_selected}")
c2.metric("Stops served", k.stops_served)
c3.metric("Unassigned", k.unassigned_stops)
c4.metric("Drive time", minutes_text(k.total_drive_minutes))
c5.metric("Violations", len(plan.hard_violations))
b1, b2, b3, b4 = st.columns(4)
if b1.button("Open in Results"):
    set_current_run(sel, ver)
    st.switch_page("pages/3_Results.py")
gate = can_export(plan)
if b2.button("Re-export Excel", disabled=not gate.allowed):
    path = export_excel(
        plan,
        rec.stops,
        rec.settings,
        Path(cfg.exports_dir),
        trucks_snapshot=rec.trucks,
        extra_warnings=rec.warnings,
        source_name=rec.source_name,
    )
    repo.record_export(sel, ver, "xlsx", str(path), plan.export_override_reason)
    st.success(f"Written to {path}")
if b3.button("Re-export PDF", disabled=not gate.allowed):
    path = export_route_sheets(
        plan, rec.stops, rec.settings, Path(cfg.exports_dir), trucks_snapshot=rec.trucks
    )
    repo.record_export(sel, ver, "pdf", str(path), plan.export_override_reason)
    st.success(f"Written to {path}")
if not gate.allowed:
    st.caption(gate.message)
if rec.deleted:
    if b4.button("Restore run"):
        repo.restore_run(sel)
        forget_record(sel)
        st.rerun()
else:
    confirm = b4.checkbox("Confirm delete", key="confirm_delete")
    if b4.button("Delete run", disabled=not confirm):
        repo.soft_delete_run(sel)
        forget_record(sel)
        st.success("Run deleted (it can be restored from the deleted list).")
        st.rerun()

exports = repo.list_exports(sel)
if exports:
    with st.expander(f"{len(exports)} export(s)"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Version": e.version,
                        "Kind": e.kind,
                        "File": e.path,
                        "When": e.created_at.strftime("%Y-%m-%d %H:%M"),
                        "Override": e.override_reason or "",
                    }
                    for e in exports
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
edits = repo.list_edits(sel)
if edits:
    with st.expander(f"{len(edits)} logged edit(s)"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Version": e.version,
                        "When": e.at.strftime("%Y-%m-%d %H:%M:%S"),
                        "Who": e.who,
                        "Operation": e.operation,
                        "Before": str(e.before),
                        "After": str(e.after),
                    }
                    for e in edits
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
if len(versions) > 1:
    with st.expander("Differences between versions"):
        va = st.selectbox("From", versions, index=0, key="diff_a")
        vb = st.selectbox("To", versions, index=len(versions) - 1, key="diff_b")
        lines = diff_plans(rec.plans[va], rec.plans[vb])
        st.write(
            "\n".join(f"• {line}" for line in lines) if lines else "No structural differences."
        )
if rec.source_bytes:
    st.download_button(
        "Download the original upload",
        data=rec.source_bytes,
        file_name=rec.source_name or f"{sel}.xlsx",
    )

st.subheader("Compare two runs")
x1, x2 = st.columns(2)
ra = x1.selectbox("Run A", run_ids, index=0, key="cmp_a")
rb = x2.selectbox("Run B", run_ids, index=min(1, len(run_ids) - 1), key="cmp_b")
if ra and rb:
    pa, pb = load_record(ra).plans, load_record(rb).plans
    ka, kb = pa[max(pa)].kpis, pb[max(pb)].kpis
    fields = [
        ("Trucks used", "trucks_used"),
        ("Loads", "loads"),
        ("Stops served", "stops_served"),
        ("D stops served %", "d_stops_served_pct"),
        ("Unassigned stops", "unassigned_stops"),
        ("Drive minutes", "total_drive_minutes"),
        ("Miles", "total_miles"),
        ("Avg on-duty min", "avg_on_duty_minutes"),
        ("Max on-duty min", "max_on_duty_minutes"),
        ("Fleet volume use %", "fleet_volume_utilization_pct"),
        ("Stops per load", "avg_stops_per_load"),
    ]
    st.dataframe(
        pd.DataFrame(
            [{"KPI": label, ra: getattr(ka, attr), rb: getattr(kb, attr)} for label, attr in fields]
        ),
        hide_index=True,
        use_container_width=True,
    )

st.subheader("Maintenance")
if st.button("Clear travel-time cache (keeps matrices used by saved runs)"):
    n = repo.clear_travel_cache()
    st.success(f"Removed {n} cached matrices.")
