"""Application configuration and run settings (defaults from brief section 4.4).

* :class:`AppConfig` - process-level settings read from environment / ``.env``
  (``OSRM_URL``, ``ROUTEOPT_DB_URL``, data directory).
* :class:`RunSettings` - the per-run settings the planner adjusts in the UI.  They are
  persisted with every run so history stays reproducible.
* :func:`validate_penalty_invariants` - the config validator required by section 7.4.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from routeopt.domain import hub_today, time_to_seconds

PresetName = Literal["A", "B", "Custom"]
LunchMode = Literal["smart", "simple"]
TravelProviderChoice = Literal["auto", "osrm", "haversine"]
FirstSolution = Literal["PARALLEL_CHEAPEST_INSERTION", "PATH_CHEAPEST_ARC"]
GeocodeMode = Literal["address", "fill"]

DEFAULT_TIER_PENALTIES: dict[int, int] = {1: 100_000_000, 2: 10_000_000, 3: 1_000_000}
DEFAULT_PULL_FORWARD_VALUE_MINUTES: dict[int, int] = {1: 240, 2: 120}
PRESET_A_FIXED_COST = 20_000
PRESET_B_SPAN_COEFFICIENT = 10


class AppConfig(BaseSettings):
    """Process-level configuration.  No secrets are needed anywhere in RouteOpt."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    osrm_url: str = Field(default="http://localhost:5000", alias="OSRM_URL")
    db_url: str = Field(default="", alias="ROUTEOPT_DB_URL")
    data_dir: Path = Field(default=Path("data"), alias="ROUTEOPT_DATA_DIR")
    log_level: str = Field(default="INFO", alias="ROUTEOPT_LOG_LEVEL")
    osrm_max_table_size: int = Field(default=2000, alias="OSRM_MAX_TABLE_SIZE")
    osrm_timeout_s: float = Field(default=20.0, alias="OSRM_TIMEOUT_S")
    map_component: str = Field(default="html", alias="ROUTEOPT_MAP_COMPONENT")  # html | st_folium

    @property
    def effective_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        path = (self.data_dir / "routeopt.db").resolve()
        return f"sqlite:///{path.as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


class RunSettings(BaseModel):
    """Per-run settings (brief 4.4).  Every field has a planner-facing tooltip in the UI."""

    plan_date: dt.date = Field(default_factory=lambda: hub_today() + dt.timedelta(days=2))
    pull_forward_days: list[int] = Field(default_factory=list)  # subset of [1, 2]
    trucks_selected: list[str] = Field(default_factory=list)  # empty = all active trucks
    shift_start: dt.time = dt.time(8, 0)
    on_duty_hours: float = 10.0
    truck_volume_cuft: float = 600.0
    truck_weight_lb_cap: float | None = None
    truck_pieces_cap: int | None = None
    stop_buffer_minutes: int = 20
    reload_minutes: int = 60
    lunch_minutes: int = 30
    lunch_earliest: dt.time | None = dt.time(11, 0)
    lunch_latest: dt.time | None = dt.time(14, 0)
    lunch_mode: LunchMode = "smart"
    max_loads_per_truck: int = 3
    preset: PresetName = "A"
    solve_time_seconds: int = 60
    eta_window_hours: float = 2.0
    travel_provider: TravelProviderChoice = "auto"
    haversine_road_factor: float = 1.3
    haversine_avg_speed_mph: float = 30.0
    geocode_mode: GeocodeMode = "address"
    same_location_travel_minutes: int = 10
    pull_forward_value_minutes: dict[int, int] = Field(
        default_factory=lambda: dict(DEFAULT_PULL_FORWARD_VALUE_MINUTES)
    )
    tier_penalties: dict[int, int] = Field(default_factory=lambda: dict(DEFAULT_TIER_PENALTIES))
    truck_fixed_cost: int | None = None  # Custom preset only
    span_cost_coefficient: int | None = None  # Custom preset only
    coverage_radius_miles: float = 75.0
    first_solution_strategy: FirstSolution = "PARALLEL_CHEAPEST_INSERTION"
    time_drift_tolerance_s: int = 60

    # ---- validation -----------------------------------------------------------------
    @field_validator("pull_forward_days")
    @classmethod
    def _check_pull_forward_days(cls, v: list[int]) -> list[int]:
        bad = [d for d in v if d not in (1, 2)]
        if bad:
            raise ValueError("Pull-forward days can only be D+1 and/or D+2.")
        return sorted(set(v))

    @field_validator("solve_time_seconds")
    @classmethod
    def _check_solve_time(cls, v: int) -> int:
        if not 1 <= v <= 60:  # the UI offers 10-60; tests use shorter solves
            raise ValueError("Solve time must be between 1 and 60 seconds.")
        return v

    @field_validator("max_loads_per_truck")
    @classmethod
    def _check_max_loads(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("Loads per truck must be between 1 and 5.")
        return v

    @field_validator("same_location_travel_minutes")
    @classmethod
    def _check_same_location_minutes(cls, v: int) -> int:
        if not 0 <= v <= 60:
            raise ValueError("Same-location travel time must be between 0 and 60 minutes.")
        return v

    @field_validator("on_duty_hours")
    @classmethod
    def _check_on_duty(cls, v: float) -> float:
        if not 1 <= v <= 16:
            raise ValueError("On-duty hours must be between 1 and 16.")
        return v

    @field_validator("truck_volume_cuft")
    @classmethod
    def _check_volume(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Truck volume must be positive.")
        return v

    @model_validator(mode="after")
    def _check_lunch_window(self) -> RunSettings:
        if (
            self.lunch_earliest is not None
            and self.lunch_latest is not None
            and self.lunch_latest < self.lunch_earliest
        ):
            raise ValueError("Lunch window: latest start must not be before earliest start.")
        return self

    # ---- derived values -------------------------------------------------------------
    @property
    def shift_start_s(self) -> int:
        """Shift start as seconds since midnight."""
        return time_to_seconds(self.shift_start)

    @property
    def on_duty_seconds(self) -> int:
        return round(self.on_duty_hours * 3600)

    @property
    def reload_seconds(self) -> int:
        return self.reload_minutes * 60

    @property
    def lunch_seconds(self) -> int:
        return self.lunch_minutes * 60

    @property
    def stop_buffer_seconds(self) -> int:
        return self.stop_buffer_minutes * 60

    @property
    def volume_cap_tenths(self) -> int:
        """Truck volume cap in tenths of a cubic foot (solver units)."""
        return round(self.truck_volume_cuft * 10)

    @property
    def eta_window_seconds(self) -> int:
        return round(self.eta_window_hours * 3600)

    def lunch_window_rel(self) -> tuple[int, int]:
        """Lunch *start* window relative to shift start, in seconds, clipped to the shift."""
        earliest = 0
        latest = max(0, self.on_duty_seconds - self.lunch_seconds)
        if self.lunch_earliest is not None:
            earliest = max(0, time_to_seconds(self.lunch_earliest) - self.shift_start_s)
        if self.lunch_latest is not None:
            latest = max(0, time_to_seconds(self.lunch_latest) - self.shift_start_s)
        latest = min(latest, max(0, self.on_duty_seconds - self.lunch_seconds))
        earliest = min(earliest, latest)
        return earliest, latest

    @property
    def effective_truck_fixed_cost(self) -> int:
        if self.preset == "A":
            return PRESET_A_FIXED_COST
        if self.preset == "B":
            return 0
        return int(
            self.truck_fixed_cost if self.truck_fixed_cost is not None else PRESET_A_FIXED_COST
        )

    @property
    def effective_span_cost_coefficient(self) -> int:
        if self.preset == "A":
            return 0
        if self.preset == "B":
            return PRESET_B_SPAN_COEFFICIENT
        return int(self.span_cost_coefficient if self.span_cost_coefficient is not None else 0)

    def penalty_for_tier(self, tier: int) -> int:
        """Drop penalty for a stop tier (brief 7.4).

        Pull-forward penalties are expressed in *minutes of extra driving*; under presets
        with a span cost the penalty is scaled by (1 + span coefficient) so the planner's
        "added if it costs less than N minutes" reading stays true.
        """
        if tier in (1, 2, 3):
            return int(self.tier_penalties.get(tier, DEFAULT_TIER_PENALTIES[tier]))
        offset = tier - 3  # tier 4 -> D+1, tier 5 -> D+2
        minutes_value = self.pull_forward_value_minutes.get(
            offset, DEFAULT_PULL_FORWARD_VALUE_MINUTES[offset]
        )
        return int(minutes_value * 60 * (1 + self.effective_span_cost_coefficient))

    def pull_forward_offsets(self) -> list[int]:
        return list(self.pull_forward_days)


def validate_penalty_invariants(settings: RunSettings, n_trucks: int) -> list[str]:
    """Config validator for the tier/penalty ordering rules of brief section 7.4.

    Returns a list of plain-English problems (empty = OK).  The rules:

    * tier 1 > tier 2 > tier 3, each at least 10x the next;
    * tier 3 > (sum of all possible fixed truck costs + T x on-duty seconds), and also
      greater than the largest cost serving a single stop could add (one truck's fixed
      cost + a full day of arc cost + a full day of span cost) so no D stop is ever dropped
      for cost reasons;
    * pull-forward penalties (tiers 4, 5) < the fixed truck cost of Preset A so a
      pull-forward never opens a truck on its own;
    * tier 4 > tier 5.
    """
    problems: list[str] = []
    p1, p2, p3 = (settings.penalty_for_tier(t) for t in (1, 2, 3))
    p4, p5 = settings.penalty_for_tier(4), settings.penalty_for_tier(5)
    if not (p1 > p2 > p3):
        problems.append("Tier penalties must be strictly decreasing: OBS > BUILDER > LTL.")
    if p1 < 10 * p2 or p2 < 10 * p3:
        problems.append("Each D-stop tier penalty must be at least 10x the next tier's penalty.")
    fixed = settings.effective_truck_fixed_cost
    span = settings.effective_span_cost_coefficient
    duty = settings.on_duty_seconds
    fleet_bound = n_trucks * fixed + n_trucks * duty
    marginal_bound = fixed + duty * (1 + span)
    if p3 <= fleet_bound:
        problems.append(
            "The LTL (tier 3) penalty must exceed the total fixed truck cost plus a full "
            f"day of driving for every truck ({fleet_bound:,}); otherwise a D stop could be "
            "dropped just to save cost."
        )
    if p3 <= marginal_bound:
        problems.append(
            "The LTL (tier 3) penalty must exceed the cost of opening one more truck for a "
            f"full day ({marginal_bound:,}); otherwise a D stop could be dropped to save cost."
        )
    raw4 = settings.pull_forward_value_minutes.get(1, DEFAULT_PULL_FORWARD_VALUE_MINUTES[1]) * 60
    raw5 = settings.pull_forward_value_minutes.get(2, DEFAULT_PULL_FORWARD_VALUE_MINUTES[2]) * 60
    if raw4 >= PRESET_A_FIXED_COST or raw5 >= PRESET_A_FIXED_COST:
        problems.append(
            "Pull-forward values must stay below the Preset A fixed truck cost "
            f"({PRESET_A_FIXED_COST // 60} minutes) so a pull-forward stop never opens a truck "
            "on its own."
        )
    if settings.preset == "Custom" and fixed > 0 and (p4 >= fixed or p5 >= fixed):
        problems.append(
            "Pull-forward values (scaled by the span cost) must stay below the custom fixed "
            "truck cost so a pull-forward stop never opens a truck on its own."
        )
    if p4 <= p5:
        problems.append("The D+1 pull-forward value must be higher than the D+2 value.")
    if p3 <= p4:
        problems.append("Pull-forward values must be far below the LTL (tier 3) penalty.")
    return problems


def preset_description(preset: PresetName) -> str:
    return {
        "A": "Fewest trucks (packed): use as few trucks as possible, each as full as possible.",
        "B": "Balanced: use the selected trucks and minimize the latest return time so days are even.",
        "Custom": "Custom: set the fixed cost per truck, the span cost, and pull-forward values yourself.",
    }[preset]


SETTING_TOOLTIPS: dict[str, str] = {
    "plan_date": "The delivery day you are planning (D). Today is D-2.",
    "pull_forward_days": (
        "Also try to fit shipments due on D+1 and/or D+2 if there is spare room; "
        "D stops are never displaced."
    ),
    "trucks_selected": "Which trucks (and their drivers) are available on the delivery day.",
    "shift_start": "When trucks leave the hub with their first, pre-loaded load.",
    "on_duty_hours": (
        "Longest allowed driver day, from shift start to the return of the last load, "
        "including lunch and reloads."
    ),
    "truck_volume_cuft": (
        "Usable cargo volume of one truck in cubic feet; the truck is never fuller than "
        "this at any point."
    ),
    "truck_weight_lb_cap": "Optional: maximum weight on a truck. Leave empty to only report weight.",
    "truck_pieces_cap": "Optional: maximum number of pieces on a truck. Leave empty to only report pieces.",
    "stop_buffer_minutes": "Extra minutes added once per stop for parking, paperwork and walking the product in.",
    "reload_minutes": "Minutes at the hub between two loads of the same truck (unload returns, load the next set).",
    "lunch_minutes": "Length of the driver's lunch break.",
    "lunch_window": "Earliest and latest time a lunch break may start. Leave empty for any time in the shift.",
    "max_loads_per_truck": "Upper limit on how many times a truck may come back to reload in one day.",
    "preset": "A = fewest trucks, packed full. B = spread the work evenly across the selected trucks.",
    "solve_time_seconds": "How long the optimizer may think. Longer usually means a slightly better plan.",
    "eta_window_hours": (
        "The customer window quoted around each ETA (ETA plus or minus this many hours)."
    ),
    "travel_provider": "Auto uses road travel times from OSRM when it is running, otherwise straight-line estimates.",
    "haversine_road_factor": (
        "Straight-line distance is multiplied by this to approximate road distance "
        "(estimates only)."
    ),
    "haversine_avg_speed_mph": "Average speed assumed for straight-line estimates (estimates only).",
    "geocode_mode": (
        "Address = geocode every row from its street address and use the file's latitude/longitude "
        "only when the address cannot be matched. Fill = keep the file's coordinates and geocode "
        "only rows without them."
    ),
    "same_location_travel_minutes": (
        "Minimum driving minutes between two different stops that sit on the same point "
        "(e.g. both placed on a ZIP centroid). 0 = use the map's travel time as is."
    ),
    "pull_forward_value_minutes": (
        "A pull-forward stop is added if it costs less than this many minutes of extra driving."
    ),
    "truck_fixed_cost": "Custom only: cost of using one more truck, in seconds of driving. Higher = fewer trucks.",
    "span_cost_coefficient": "Custom only: weight on the latest return time. Higher = more even days.",
    "lunch_mode": "Smart lets lunch coincide with a reload; Simple always books an explicit 30-minute break.",
    "first_solution_strategy": "How the optimizer builds its first plan before improving it.",
}
