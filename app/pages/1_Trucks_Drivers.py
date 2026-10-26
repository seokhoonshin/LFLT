"""Trucks & Drivers - editable table (brief 4.5)."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd
import streamlit as st
from routeopt.domain import TruckConfig

from _common import fallback_banner, get_repo, osrm_status_line, setup_page

setup_page("Trucks & Drivers")
osrm_status_line()
fallback_banner()

st.title("Trucks & Drivers")
st.caption(
    "Truck numbers, the driver assigned to each, and whether the truck is available. "
    "Every run keeps its own snapshot of this table, so history stays correct if drivers change."
)

repo = get_repo()
trucks = repo.list_trucks()
df = pd.DataFrame(
    [{"Truck": t.truck_number, "Driver": t.driver_name, "Active": t.active} for t in trucks]
)
edited = st.data_editor(
    df,
    num_rows="dynamic",
    hide_index=True,
    use_container_width=True,
    column_config={
        "Truck": st.column_config.TextColumn(
            "Truck", help="Truck number, e.g. T03", required=True, max_chars=16
        ),
        "Driver": st.column_config.TextColumn("Driver", help="Driver name printed on route sheets"),
        "Active": st.column_config.CheckboxColumn(
            "Active", help="Untick when the truck is out of service"
        ),
    },
    key="trucks_editor",
)

col1, col2 = st.columns([1, 3])
if col1.button("Save trucks", type="primary"):
    rows = [
        TruckConfig(
            truck_number=str(r["Truck"]).strip().upper(),
            driver_name=str(r["Driver"] or "").strip() if pd.notna(r["Driver"]) else "",
            active=bool(r["Active"]),
        )
        for _, r in edited.iterrows()
        if str(r["Truck"] or "").strip()
    ]
    numbers = [t.truck_number for t in rows]
    if len(numbers) != len(set(numbers)):
        st.error("Two rows have the same truck number. Please make them unique.")
    elif not rows:
        st.error("Keep at least one truck.")
    else:
        repo.save_trucks(rows)
        st.success(f"Saved {len(rows)} trucks.")
        st.rerun()
col2.caption(f"{sum(1 for t in trucks if t.active)} of {len(trucks)} trucks are active.")
