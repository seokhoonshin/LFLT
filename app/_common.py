"""Shared helpers for the Streamlit pages (no business rules live here)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routeopt.config import AppConfig, RunSettings
from routeopt.db.repo import Repository, RunRecord
from routeopt.domain import Plan, Stop, clock, hub_today
from routeopt.logging_utils import configure_logging
from routeopt.travel import FALLBACK_WARNING
from routeopt.travel.osrm import OSRMProvider

PAGE_ICON = "🚚"


def setup_page(title: str, layout: str = "wide") -> None:
    st.set_page_config(page_title=f"RouteOpt · {title}", page_icon=PAGE_ICON, layout=layout)  # type: ignore[arg-type]
    configure_logging(get_config().log_level)


@st.cache_resource
def get_config() -> AppConfig:
    return AppConfig()


@st.cache_resource
def get_repo() -> Repository:
    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    cfg.exports_dir.mkdir(parents=True, exist_ok=True)
    repo = Repository(cfg.effective_db_url)
    repo.init_schema()
    return repo


@st.cache_data(ttl=30)
def osrm_healthy() -> bool:
    cfg = get_config()
    try:
        return OSRMProvider(cfg.osrm_url, timeout_s=cfg.osrm_timeout_s).healthy()
    except Exception:  # pragma: no cover - defensive
        return False


def osrm_status_line() -> None:
    cfg = get_config()
    if osrm_healthy():
        st.sidebar.success(f"OSRM road network: online ({cfg.osrm_url})", icon="🗺️")
    else:
        st.sidebar.warning(
            f"OSRM is not responding at {cfg.osrm_url}. New runs will use straight-line "
            "estimates until it is back.",
            icon="⚠️",
        )


def fallback_banner(plan: Plan | None = None) -> None:
    """Persistent banner whenever estimated travel times are in play."""
    if plan is not None and plan.matrix_source != "osrm":
        st.warning(f"{FALLBACK_WARNING}. ETAs on this plan are straight-line estimates.", icon="⚠️")
    elif plan is None and st.session_state.get("travel_fallback"):
        st.warning(f"{FALLBACK_WARNING}. The last run used straight-line estimates.", icon="⚠️")


# ---- session state ------------------------------------------------------------------


def set_current_run(run_id: str, version: int) -> None:
    st.session_state["current_run_id"] = run_id
    st.session_state["current_version"] = version


def current_run_id() -> str | None:
    return st.session_state.get("current_run_id")


def current_version() -> int:
    return int(st.session_state.get("current_version", 0))


def load_record(run_id: str, refresh: bool = False) -> RunRecord:
    """Load a run (cached in the session so navigation between pages stays snappy)."""
    cache: dict[str, RunRecord] = st.session_state.setdefault("run_records", {})
    if refresh or run_id not in cache:
        cache[run_id] = get_repo().load_run(run_id)
    return cache[run_id]


def forget_record(run_id: str) -> None:
    st.session_state.get("run_records", {}).pop(run_id, None)


# ---- formatting -----------------------------------------------------------------------


def minutes_text(m: float) -> str:
    h, mm = divmod(round(m), 60)
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


def status_text(status: str) -> str:
    return {
        "ROUTING_SUCCESS": "finished (local optimum reached)",
        "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED": "stopped at the time limit",
        "ROUTING_FAIL": "no plan found",
        "ROUTING_FAIL_TIMEOUT": "no plan found within the time limit",
        "ROUTING_INVALID": "the model was invalid",
        "ROUTING_INFEASIBLE": "no feasible plan exists",
        "ROUTING_OPTIMAL": "finished (optimal)",
        "NO_STOPS": "nothing to plan",
    }.get(status, status)


def tier_label(tier: int) -> str:
    return {
        1: "Tier 1 · D · OBS homes",
        2: "Tier 2 · D · Builder",
        3: "Tier 3 · D · LTL",
        4: "Tier 4 · D+1 pull-forward",
        5: "Tier 5 · D+2 pull-forward",
    }.get(tier, f"Tier {tier}")


def load_table(
    plan: Plan, truck_number: str, load_seq: int, stops_by_id: dict[str, Stop]
) -> pd.DataFrame:
    truck = plan.truck(truck_number)
    load = next(load for load in truck.loads if load.seq == load_seq)
    rows: list[dict[str, Any]] = []
    for v in load.stops:
        s = stops_by_id.get(v.stop_id)
        rows.append(
            {
                "#": v.seq,
                "ETA": clock(v.arrive_s),
                "Window": f"{clock(v.eta_window_start_s)} – {clock(v.eta_window_end_s)}",
                "Customer": s.customer_label if s else v.stop_id,
                "Address": f"{s.address}, {s.city} {s.zip}" if s else "",
                "Type": "/".join(sorted({sh.shipment_type.value for sh in s.shipments}))
                if s
                else "",
                "Service min": s.service_total_minutes if s else "",
                "Depart": clock(v.depart_s),
                "Volume after": v.volume_after,
                "Lunch before": "🍽️" if v.lunch_before else "",
                "Stop": v.stop_id,
            }
        )
    return pd.DataFrame(rows)


def settings_from_plan(plan: Plan) -> RunSettings:
    return RunSettings.model_validate(plan.settings)


def today_plus(days: int) -> dt.date:
    """Today at the hub (not the browser or server clock) plus ``days``."""
    return hub_today() + dt.timedelta(days=days)


def kpi_cards(plan: Plan) -> None:
    k = plan.kpis
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Trucks used", f"{k.trucks_used} / {k.trucks_selected}")
    c2.metric("Loads", k.loads)
    c3.metric("Stops served", f"{k.stops_served}", help=f"{k.shipments_served} shipments")
    c4.metric(
        "D stops served",
        f"{k.d_stops_served} / {k.d_stops_total}",
        f"{k.d_stops_served_pct}%",
        delta_color="off",
    )
    c5.metric(
        "Unassigned",
        k.unassigned_stops,
        help=", ".join(f"{t}: {n}" for t, n in k.unassigned_by_tier.items()) or "none",
    )
    c6.metric("Fleet volume use", f"{k.fleet_volume_utilization_pct}%")
    d1, d2, d3, d4, d5, d6 = st.columns(6)
    d1.metric("Drive time", minutes_text(k.total_drive_minutes))
    d2.metric("Miles", f"{k.total_miles:,.0f}")
    d3.metric("Avg on duty", minutes_text(k.avg_on_duty_minutes))
    d4.metric("Longest day", minutes_text(k.max_on_duty_minutes))
    d5.metric("Stops per load", k.avg_stops_per_load)
    pf = ", ".join(
        f"{d}: {n}/{k.pull_forward_available.get(d, 0)}"
        for d, n in k.pull_forward_included.items()
        if k.pull_forward_available.get(d, 0)
    )
    d6.metric("Pull-forward", pf or "off")
