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

from routeopt.appointments import ceil_half_hour, pickup_seconds
from routeopt.config import AppConfig, RunSettings
from routeopt.db.repo import Repository, RunRecord
from routeopt.domain import SITE_BY_CODE, Plan, Stop, clock, hub_today
from routeopt.logging_utils import configure_logging
from routeopt.travel import FALLBACK_WARNING
from routeopt.travel.osrm import OSRMProvider
from routeopt.travel.tomtom import is_road_source

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
    if cfg.tomtom_api_key:
        st.sidebar.info("TomTom key configured (Travel times = TomTom).", icon="🚚")


def fallback_banner(plan: Plan | None = None) -> None:
    """Persistent banner whenever estimated travel times are in play."""
    if plan is not None and not is_road_source(plan.matrix_source):
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
    site = SITE_BY_CODE.get(load.site)
    settings = settings_from_plan(plan)
    # first row: the warehouse pickup = the first stop's ETA (rounded up to the half hour)
    # minus the drive minus an hour of loading; load 1 never before the DC opens
    first = load.stops[0] if load.stops else None
    pickup = None
    if first is not None:
        earliest = settings.earliest_shift_start_s if load.seq == 1 else 0
        drive_s = round(first.travel_min_from_prev * 60)
        pickup = pickup_seconds(ceil_half_hour(first.arrive_s), drive_s, earliest)
    rows: list[dict[str, Any]] = [
        {
            "#": "DC",
            "ETA": clock(pickup) if pickup is not None else "",
            "Window": "",
            "Customer": f"{load.site} warehouse pickup",
            "Address": site.address if site else "",
            "Type": "",
            "Drive min": None,
            "Stop transit min": None,
            "Miles from prev": None,
            "Service min": None,
            "Depart": clock(load.depart_s),
            "Stop volume": round(load.load_out_volume, 1),
            "Volume after": round(load.load_out_volume, 1),
            "Lunch before": "",
            "Appointment contact": "",
            "Stop": "",
        }
    ]
    for v in load.stops:
        s = stops_by_id.get(v.stop_id)
        rows.append(
            {
                "#": str(v.seq),
                "ETA": clock(ceil_half_hour(v.arrive_s)),  # rounded up to the half hour
                "Window": f"{clock(v.eta_window_start_s)} – {clock(v.eta_window_end_s)}",
                "Customer": s.customer_label if s else v.stop_id,
                "Address": f"{s.address}, {s.city} {s.zip}" if s else "",
                "Type": "/".join(sorted({sh.shipment_type.value for sh in s.shipments}))
                if s
                else "",
                "Drive min": v.travel_min_from_prev,
                "Stop transit min": v.transit_min_from_prev,
                "Miles from prev": v.dist_mi_from_prev,
                "Service min": s.service_total_minutes if s else "",
                "Depart": clock(v.depart_s),
                "Stop volume": round(v.volume_on_arrival - v.volume_after, 1),
                "Volume after": v.volume_after,
                "Lunch before": "🍽️" if v.lunch_before else "",
                "Appointment contact": s.contact if s else "",
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


# ---- team sharing (Appointments / Analyze through a git branch) ----------------------------
def team_share() -> bool:
    """Sync the Appointments / Analyze data with the team branch when OPENMILE_ROLE is set
    (call at the top of a page).  Returns True when this PC is a read-only viewer."""
    from routeopt.share import ShareError, sync

    cfg = get_config()
    role = {"publisher": "editor"}.get(
        cfg.share_role.strip().lower(), cfg.share_role.strip().lower()
    )
    if role not in ("editor", "viewer"):
        return False
    import time

    last = st.session_state.get("share_pulled_at", 0.0)
    pull = time.monotonic() - last > 20  # look for others' saves at most every 20 s
    try:
        status = sync(get_repo(), ROOT, role, cfg.share_branch, cfg.share_remote, pull=pull)
    except ShareError as exc:
        st.sidebar.warning(f"Team sharing is off right now: {exc}")
        return role == "viewer"
    if pull:
        st.session_state["share_pulled_at"] = time.monotonic()
    if status == "conflict":
        st.warning(
            "Someone else saved at the same time: their version is loaded and your last "
            "change was not saved. Please make it again."
        )
    elif status == "loaded":
        st.toast("Loaded the team's latest appointments / analyze data.")
    st.sidebar.caption(
        f"👥 Team sharing: **{'view only' if role == 'viewer' else 'editing'}** · "
        f"synced {dt.datetime.now():%H:%M:%S}"
    )
    return role == "viewer"
