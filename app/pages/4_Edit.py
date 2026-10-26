"""Edit - move / re-sequence / unassign / assign stops, re-validate, save versions (brief 10)."""

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
from routeopt.db.repo import EditLogEntry
from routeopt.domain import Plan, clock
from routeopt.edit.operations import (
    EditContext,
    EditError,
    assign_stop,
    move_down,
    move_stop,
    move_up,
    reorder_stop,
    unassign_stop,
)
from routeopt.edit.revalidate import (
    can_export,
    diff_plans,
    new_version,
    revalidate,
    set_export_override,
    violations_by_stop,
)
from routeopt.export.excel import export_excel
from routeopt.export.route_sheets_pdf import export_route_sheets

from _common import (
    current_run_id,
    current_version,
    fallback_banner,
    forget_record,
    get_config,
    get_repo,
    kpi_cards,
    load_record,
    load_table,
    minutes_text,
    osrm_status_line,
    set_current_run,
    setup_page,
)

setup_page("Edit")
osrm_status_line()
repo = get_repo()
cfg = get_config()

run_id = current_run_id()
if not run_id:
    st.info("Open a run on the **Results** page first, then press *Edit this plan*.")
    st.stop()
rec = load_record(run_id)
versions = sorted(rec.plans)
base_version = current_version() if current_version() in versions else versions[-1]
stops = rec.stops
by_id = {s.stop_id: s for s in stops}
settings = rec.settings

st.title(f"Edit — run {run_id}")
fallback_banner(rec.plans[base_version])

# ---- working copy in session state ------------------------------------------------------
work_key = f"edit_{run_id}"
state = st.session_state.get(work_key)
if state is None or state["base_version"] != base_version:
    state = {
        "base_version": base_version,
        "plan": rec.plans[base_version].model_copy(deep=True),
        "log": [],
        "dirty": False,
    }
    st.session_state[work_key] = state
plan: Plan = state["plan"]
ctx = EditContext(plan=plan, stops=stops, settings=settings, matrix=rec.matrix)

st.caption(
    f"Editing from **v{base_version}** · {len(state['log'])} unsaved change(s). Every change is re-checked "
    "with the cached travel times; times, windows, volumes and KPIs are recomputed immediately."
)


def _apply(result: tuple[Plan, EditLogEntry]) -> None:
    new_plan, entry = result
    state["plan"] = revalidate(EditContext(new_plan, stops, settings, rec.matrix), new_plan)
    state["log"].append(entry)
    state["dirty"] = True
    st.rerun()


# ---- violations ----------------------------------------------------------------------------
hard = plan.hard_violations
if hard:
    st.error(f"{len(hard)} hard violation(s):")
    for v in hard:
        st.write(f"• {v}")
soft = [v for v in plan.violations if v.severity == "warning"]
if soft:
    with st.expander(f"{len(soft)} note(s)"):
        for v in soft:
            st.write(f"• {v}")
if not plan.violations:
    st.success("No violations.")

kpi_cards(plan)
flags = violations_by_stop(plan)

# ---- operations ---------------------------------------------------------------------------
st.subheader("Change the plan")
served = [(t.number, load.seq, v) for t in plan.trucks for load in t.loads for v in load.stops]
served_ids = [v.stop_id for _t, _l, v in served]
unassigned_ids = [u.stop_id for u in plan.unassigned] + [u.stop_id for u in plan.out_of_scope]
truck_numbers = [t.number for t in plan.trucks]


def _label(stop_id: str) -> str:
    s = by_id.get(stop_id)
    where = next((f"{t} L{l} #{v.seq}" for t, l, v in served if v.stop_id == stop_id), "unassigned")
    return f"{stop_id} · {s.customer_label if s else ''} ({where})"


tab_move, tab_seq, tab_unassign, tab_assign = st.tabs(
    ["Move to another load", "Re-sequence", "Unassign", "Assign an unassigned stop"]
)
with tab_move:
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    sid = (
        c1.selectbox("Stop", served_ids, format_func=_label, key="mv_stop") if served_ids else None
    )
    to_truck = c2.selectbox("To truck", truck_numbers, key="mv_truck")
    n_loads = len([load for load in plan.truck(to_truck).loads if load.stops]) if to_truck else 0
    load_options = list(range(1, n_loads + 1)) + (
        [n_loads + 1] if n_loads < settings.max_loads_per_truck else []
    )
    to_load = c3.selectbox(
        "To load",
        load_options,
        format_func=lambda x: f"load {x}" + (" (new)" if x == n_loads + 1 else ""),
        key="mv_load",
    )
    max_pos = (
        len(next((load.stops for load in plan.truck(to_truck).loads if load.seq == to_load), []))
        + 1
        if to_truck
        else 1
    )
    pos = c4.number_input("Position", 1, max(1, max_pos), max(1, max_pos), key="mv_pos")
    if st.button("Move", key="mv_btn", disabled=sid is None):
        try:
            _apply(move_stop(ctx, str(sid), to_truck, int(to_load), int(pos) - 1))
        except EditError as exc:
            st.error(str(exc))
with tab_seq:
    c1, c2, c3 = st.columns([2, 1, 1])
    sid2 = (
        c1.selectbox("Stop", served_ids, format_func=_label, key="seq_stop") if served_ids else None
    )
    if sid2:
        where = next((t, l, v) for t, l, v in served if v.stop_id == sid2)
        new_pos = c2.number_input(
            "New position",
            1,
            len(next(load.stops for load in plan.truck(where[0]).loads if load.seq == where[1])),
            where[2].seq,
            key="seq_pos",
        )
        b1, b2, b3 = c3.columns(3)
        try:
            if b1.button("▲", key="seq_up"):
                _apply(move_up(ctx, where[0], where[1], sid2))
            if b2.button("▼", key="seq_down"):
                _apply(move_down(ctx, where[0], where[1], sid2))
            if b3.button("Set", key="seq_set"):
                _apply(reorder_stop(ctx, where[0], where[1], sid2, int(new_pos) - 1))
        except EditError as exc:
            st.error(str(exc))
with tab_unassign:
    sid3 = (
        st.selectbox("Stop to take off its route", served_ids, format_func=_label, key="un_stop")
        if served_ids
        else None
    )
    if st.button("Unassign", key="un_btn", disabled=sid3 is None):
        try:
            _apply(unassign_stop(ctx, str(sid3)))
        except EditError as exc:
            st.error(str(exc))
with tab_assign:
    if not unassigned_ids:
        st.info("There are no unassigned stops.")
    else:
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        sid4 = c1.selectbox("Unassigned stop", unassigned_ids, format_func=_label, key="as_stop")
        a_truck = c2.selectbox("To truck", truck_numbers, key="as_truck")
        n_loads_a = len([load for load in plan.truck(a_truck).loads if load.stops])
        a_options = list(range(1, n_loads_a + 1)) + (
            [n_loads_a + 1] if n_loads_a < settings.max_loads_per_truck else []
        )
        a_load = c3.selectbox(
            "To load",
            a_options,
            format_func=lambda x: f"load {x}" + (" (new)" if x == n_loads_a + 1 else ""),
            key="as_load",
        )
        a_max = (
            len(next((load.stops for load in plan.truck(a_truck).loads if load.seq == a_load), []))
            + 1
        )
        a_pos = c4.number_input("Position", 1, max(1, a_max), max(1, a_max), key="as_pos")
        if st.button("Assign", key="as_btn"):
            try:
                _apply(assign_stop(ctx, sid4, a_truck, int(a_load), int(a_pos) - 1))
            except EditError as exc:
                st.error(str(exc))

# ---- current routes with inline highlighting ----------------------------------------------
st.subheader("Routes")
for truck in plan.trucks:
    loads = [load for load in truck.loads if load.stops]
    if not loads:
        continue
    truck_flags = [
        f
        for key, fs in flags.items()
        if key == truck.number or key.startswith(f"{truck.number}:")
        for f in fs
    ]
    title = f"{truck.number} · {truck.driver_name or 'no driver'} — {len(loads)} load(s), on duty {minutes_text(truck.on_duty_minutes)}, lunch {truck.lunch.where}"
    if truck_flags:
        title = "🔴 " + title
    with st.expander(title, expanded=bool(truck_flags)):
        for f in truck_flags:
            st.error(str(f)) if f.severity == "error" else st.warning(str(f))
        for load in loads:
            st.markdown(
                f"**Load {load.seq}** · {clock(load.depart_s)} → {clock(load.return_s)} · load-out {load.load_out_volume:.0f} cu ft"
            )
            df = load_table(plan, truck.number, load.seq, by_id)
            df["Problem"] = [
                " | ".join(v.message for v in flags.get(sid, [])) for sid in df["Stop"]
            ]

            def _style(row: pd.Series) -> list[str]:
                return ["background-color: #ffd6d6" if row["Problem"] else ""] * len(row)

            st.dataframe(df.style.apply(_style, axis=1), hide_index=True, use_container_width=True)
if plan.unassigned:
    st.markdown("**Unassigned**")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Stop": u.stop_id,
                    "Customer": by_id[u.stop_id].customer_label if u.stop_id in by_id else "",
                    "Tier": u.tier,
                    "Volume": u.volume,
                    "Reason": u.reason,
                }
                for u in plan.unassigned
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

# ---- save / revert / export -----------------------------------------------------------------
st.subheader("Save, revert, export")
if state["log"]:
    with st.expander(f"{len(state['log'])} unsaved change(s)"):
        for e in state["log"]:
            st.write(f"• {e.at.strftime('%H:%M:%S')} {e.operation}: {e.before} → {e.after}")
        for line in diff_plans(rec.plans[base_version], plan):
            st.caption(line)
s1, s2, s3 = st.columns(3)
note = s1.text_input("Version note (optional)", key="version_note")
if s1.button("Save as new version", type="primary", disabled=not state["dirty"]):
    next_version = max(versions) + 1
    to_save = new_version(plan, next_version)
    repo.save_plan_version(run_id, to_save, state["log"], note=note)
    forget_record(run_id)
    st.session_state.pop(work_key, None)
    set_current_run(run_id, next_version)
    st.success(f"Saved as v{next_version}.")
    st.rerun()
if s2.button("Revert to optimizer result (v0)"):
    st.session_state.pop(work_key, None)
    set_current_run(run_id, 0)
    st.rerun()
if s2.button("Discard unsaved changes", disabled=not state["dirty"]):
    st.session_state.pop(work_key, None)
    st.rerun()

gate = can_export(plan)
export_plan = plan
if not gate.allowed:
    st.warning(gate.message)
    override = s3.checkbox("Export anyway", key="export_anyway")
    reason = s3.text_input(
        "Reason (required, logged and printed on the export)",
        key="override_reason",
        disabled=not override,
    )
    if override and reason.strip():
        export_plan = set_export_override(plan, reason)
        gate = can_export(export_plan)
out_dir = Path(cfg.exports_dir)
x1, x2 = s3.columns(2)
if x1.button("Export Excel", key="edit_xlsx", disabled=not gate.allowed):
    if state["dirty"]:
        st.warning(
            "Unsaved changes: the export reflects the working copy. Save a version to keep it."
        )
    path = export_excel(
        export_plan,
        stops,
        settings,
        out_dir,
        trucks_snapshot=rec.trucks,
        extra_warnings=rec.warnings,
        source_name=rec.source_name,
    )
    repo.record_export(
        run_id, export_plan.plan_version, "xlsx", str(path), export_plan.export_override_reason
    )
    if export_plan.export_override_reason:
        repo.log_edits(
            run_id,
            export_plan.plan_version,
            [
                EditLogEntry(
                    operation="export_override",
                    before={},
                    after={"reason": export_plan.export_override_reason, "file": str(path)},
                )
            ],
        )
    st.session_state["edit_last_file"] = str(path)
if x2.button("Export PDF", key="edit_pdf", disabled=not gate.allowed):
    path = export_route_sheets(export_plan, stops, settings, out_dir, trucks_snapshot=rec.trucks)
    repo.record_export(
        run_id, export_plan.plan_version, "pdf", str(path), export_plan.export_override_reason
    )
    if export_plan.export_override_reason:
        repo.log_edits(
            run_id,
            export_plan.plan_version,
            [
                EditLogEntry(
                    operation="export_override",
                    before={},
                    after={"reason": export_plan.export_override_reason, "file": str(path)},
                )
            ],
        )
    st.session_state["edit_last_file"] = str(path)
last = st.session_state.get("edit_last_file")
if last and Path(last).exists():
    st.download_button(
        f"Download {Path(last).name}", data=Path(last).read_bytes(), file_name=Path(last).name
    )
