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

import pandas as pd
import streamlit as st
from routeopt.appointments import STATUSES

from _common import get_repo, osrm_status_line, setup_page

setup_page("Appointments")
osrm_status_line()
repo = get_repo()

st.title("Appointments")
st.caption(
    "Trucks picked with **Select this truck** on Results. Set each shipment's status as you "
    "request and confirm appointments, then press **Save**."
)

rows = repo.list_appointments()
if not rows:
    st.info("No trucks picked yet — open **Results** and press **Select this truck**.")
    st.stop()

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
    st.subheader(f"{truck} · {done}/{len(items)} confirmed")
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
                        "Status", options=STATUSES, required=True
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
        st.caption("Removes this truck's rows and their statuses (pick it again on Results).")
        if st.button(f"Remove {truck}", key=f"rm-{run_id}-{truck}"):
            repo.unselect_truck(run_id, truck)
            st.rerun()
