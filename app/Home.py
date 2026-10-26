"""RouteOpt - Streamlit entry page."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent
for _p in (_APP.parent, _APP):  # repo root (routeopt package) and app/ (_common)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import streamlit as st

from _common import (
    current_run_id,
    fallback_banner,
    get_config,
    get_repo,
    osrm_status_line,
    setup_page,
)

setup_page("Home")
osrm_status_line()
fallback_banner()

st.title("🚚 RouteOpt")
st.caption("Same-day white-glove delivery routes from the Rialto, CA hub")

repo = get_repo()
cfg = get_config()
runs = repo.list_runs(limit=5)
trucks = repo.list_trucks()

c1, c2, c3 = st.columns(3)
c1.metric("Active trucks", sum(1 for t in trucks if t.active))
c2.metric("Saved runs", len(repo.list_runs(limit=10_000)))
c3.metric("Travel times", "OSRM" if st.session_state.get("osrm_ok", True) else "estimated")

st.markdown(
    """
**How a planning day works (D-2):**

1. **Trucks & Drivers** – check who is driving which truck.
2. **New Run** – upload the shipment workbook, review the validation report, pick a preset
   (A = fewest trucks, B = balanced) and press **Optimize**.  The optimizer thinks for at
   most 60 seconds.
3. **Results** – KPIs, map, per-truck loads and the list of stops that did not fit, each
   with a plain-English reason.  Export the Excel plan and the PDF route sheets.
4. **Edit** – move, re-sequence, unassign or assign stops by hand; every change is
   re-checked immediately and saved as a new version.
5. **History** – every run and version is kept for audit and can be re-exported or compared.
"""
)

if runs:
    st.subheader("Recent runs")
    st.dataframe(
        [
            {
                "Run": r.run_id,
                "Created": r.created_at.strftime("%Y-%m-%d %H:%M"),
                "Delivery day": r.plan_date.isoformat(),
                "Preset": r.preset,
                "Trucks": f"{r.trucks_used}/{r.trucks_selected}",
                "Stops": f"{r.stops_served}/{r.stops_total}",
                "Unassigned": r.unassigned,
                "Versions": r.version_count,
                "Travel": r.matrix_source,
            }
            for r in runs
        ],
        hide_index=True,
        use_container_width=True,
    )
    if current_run_id():
        st.info(f"Current run: **{current_run_id()}** — open **Results** to continue.")
else:
    st.info("No runs yet. Start with **New Run** in the sidebar.")

st.caption(
    "Re-running the optimizer may give a slightly different plan: the search is time-limited "
    "and not perfectly repeatable. Every run is saved, so nothing is lost."
)
