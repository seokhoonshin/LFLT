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
GoalName = Literal["both", "stops", "volume", "custom"]
LunchMode = Literal["smart", "simple"]
TravelProviderChoice = Literal["auto", "osrm", "haversine", "tomtom"]
FirstSolution = Literal["PARALLEL_CHEAPEST_INSERTION", "PATH_CHEAPEST_ARC"]
ObsMode = Literal["last", "exclude", "normal"]
GeocodeMode = Literal["address", "fill"]
ProfileName = Literal["ca_lflt", "legacy"]

# Values a profile supplies for every setting the caller does not give explicitly.
# ``ca_lflt`` = CA LFLT RouteOpt Conditions (docs/CA_LFLT_RouteOpt_Conditions_EN.md);
# ``legacy`` = the original single-hub brief.
PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "ca_lflt": {
        "shift_start": dt.time(6, 0),
        "first_load_minutes": 90,
        "reload_minutes": 120,
        "floating_start": True,
        "earliest_shift_start": dt.time(6, 0),
        "earliest_delivery": dt.time(8, 0),
        "truck_volume_cuft": 800.0,
        "volume_strict": True,
        "pallet_volume_cuft": 400.0,
        "unload_minutes_per_cuft": 0.1,
        "max_loads_per_truck": 2,
        "max_one_way_miles": 200.0,
        "carrier_check_miles": 150.0,
        "two_turn_max_miles": 300.0,
        "turn_gap_minutes": 180,
        "obs_mode": "last",
        "require_shipment_suffix": False,
        "coverage_radius_miles": 200.0,
        "trucks_selected": ["T01", "T02"],
        "geocode_mode": "address",
        "same_location_travel_minutes": 15,
        "stop_transit_minutes": 20,
        "volume_value_cuft": 100.0,
        "use_priority": False,
        "goal": "both",
        "stop_buffer_minutes": 0,
        "traffic_factor": 1.3,
    },
    "legacy": {
        "shift_start": dt.time(8, 0),
        "first_load_minutes": 0,
        "reload_minutes": 60,
        "floating_start": False,
        "earliest_shift_start": dt.time(8, 0),
        "earliest_delivery": None,
        "truck_volume_cuft": 600.0,
        "volume_strict": False,
        "pallet_volume_cuft": None,
        "unload_minutes_per_cuft": None,
        "max_loads_per_truck": 3,
        "max_one_way_miles": None,
        "carrier_check_miles": None,
        "two_turn_max_miles": None,
        "turn_gap_minutes": 0,
        "obs_mode": "normal",
        "require_shipment_suffix": False,
        "coverage_radius_miles": 75.0,
        "trucks_selected": [],
        "geocode_mode": "fill",
        "same_location_travel_minutes": 0,
        "stop_transit_minutes": 0,
        "volume_value_cuft": None,
        "use_priority": True,
        "goal": "custom",
        "stop_buffer_minutes": 20,
        "traffic_factor": 1.0,
    },
}

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
    tomtom_api_key: str = Field(default="", alias="TOMTOM_API_KEY")
    tomtom_base_url: str = Field(default="https://api.tomtom.com", alias="TOMTOM_BASE_URL")
    tomtom_max_cells: int = Field(default=200, alias="TOMTOM_MAX_CELLS")  # per request
    tomtom_max_nodes: int = Field(default=60, alias="TOMTOM_MAX_NODES")  # per run
    tomtom_timeout_s: float = Field(default=60.0, alias="TOMTOM_TIMEOUT_S")

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

    profile: ProfileName = "ca_lflt"
    plan_date: dt.date = Field(default_factory=lambda: hub_today() + dt.timedelta(days=2))
    pull_forward_days: list[int] = Field(default_factory=list)  # subset of [1, 2]
    trucks_selected: list[str] = Field(default_factory=list)  # empty = all active trucks
    shift_start: dt.time = dt.time(6, 0)
    first_load_minutes: int = 90  # loading before the first departure (live loading)
    # CA: the driver starts so that the first stop is reached at the first appointment time
    # (start = appointment - first loading - drive to the first stop) and works on_duty_hours
    # from there - but never before earliest_shift_start (the DC opens then; CA 06:00), so
    # a far first stop is reached after the appointment.  Each second a load-1 first stop
    # is reached after the appointment costs late_first_stop_cost: first stops reachable
    # by the appointment are preferred.
    floating_start: bool = True
    earliest_shift_start: dt.time = dt.time(6, 0)
    late_first_stop_cost: int = 2
    earliest_delivery: dt.time | None = dt.time(8, 0)  # first appointment; early trucks wait
    on_duty_hours: float = 10.0
    truck_volume_cuft: float = 800.0
    volume_strict: bool = True  # "less than" the cap (CA) instead of "at most"
    pallet_volume_cuft: float | None = 400.0  # limit for a load with any pallet shipment
    unload_minutes_per_cuft: float | None = 0.1  # None = use the upload's service_minutes
    truck_weight_lb_cap: float | None = None
    truck_pieces_cap: int | None = None
    stop_buffer_minutes: int = 20
    reload_minutes: int = 120
    lunch_minutes: int = 30
    lunch_earliest: dt.time | None = dt.time(11, 0)
    lunch_latest: dt.time | None = dt.time(14, 0)
    lunch_mode: LunchMode = "smart"
    max_loads_per_truck: int = 2
    max_one_way_miles: float | None = 200.0  # DC to the last stop of a load ("less than")
    carrier_check_miles: float | None = 150.0  # warn: confirm with the carrier above this
    two_turn_max_miles: float | None = 300.0  # all turns of a two-turn truck ("less than")
    turn_gap_minutes: int = 180  # a turn departs >= last delivery of the previous turn + gap
    require_shipment_suffix: bool = False  # on: ids must end in _LMD/_RTN/_INC
    preset: PresetName = "A"
    solve_time_seconds: int = 60
    eta_window_hours: float = 2.0
    travel_provider: TravelProviderChoice = "auto"
    haversine_road_factor: float = 1.3
    haversine_avg_speed_mph: float = 30.0
    obs_mode: ObsMode = "last"
    obs_radius_miles: float = 5.0
    geocode_mode: GeocodeMode = "address"
    same_location_travel_minutes: int = 15
    stop_transit_minutes: int = 20  # added to every stop-to-stop drive
    # stop value = one stop + its volume / this many cu ft (None = count stops only)
    volume_value_cuft: float | None = 100.0
    use_priority: bool = False  # False: the upload's priority column is ignored (all alike)
    # which stops to keep when there are more candidates than trucks (see GOAL_WEIGHTS);
    # "custom" uses volume_value_cuft
    goal: GoalName = "both"
    traffic_factor: float = 1.3  # OSRM drive times are free-flow; x this for daytime traffic
    traffic_depart_time: dt.time = dt.time(10, 0)  # TomTom: representative departure on D
    truck_length_ft: float = 26.0  # TomTom truck routing
    truck_height_ft: float = 13.5
    truck_gross_weight_lb: float = 26000.0
    pull_forward_value_minutes: dict[int, int] = Field(
        default_factory=lambda: dict(DEFAULT_PULL_FORWARD_VALUE_MINUTES)
    )
    tier_penalties: dict[int, int] = Field(default_factory=lambda: dict(DEFAULT_TIER_PENALTIES))
    truck_fixed_cost: int | None = None  # Custom preset only
    span_cost_coefficient: int | None = None  # Custom preset only
    coverage_radius_miles: float = 200.0
    first_solution_strategy: FirstSolution = "PARALLEL_CHEAPEST_INSERTION"
    time_drift_tolerance_s: int = 60

    # ---- validation -----------------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, data: object) -> object:
        if isinstance(data, dict):
            profile = data.get("profile")
            if profile is None:
                # a run saved before profiles existed (a full dump) keeps the legacy rules
                profile = "legacy" if "time_drift_tolerance_s" in data else "ca_lflt"
            if profile not in PROFILE_DEFAULTS:
                raise ValueError(f"Unknown profile '{profile}' (use ca_lflt or legacy).")
            data = {**PROFILE_DEFAULTS[str(profile)], **data, "profile": profile}
        return data

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

    @field_validator("obs_radius_miles")
    @classmethod
    def _check_obs_radius(cls, v: float) -> float:
        if not 0 < v <= 50:
            raise ValueError("OBS radius must be between 0 and 50 miles.")
        return v

    @field_validator("traffic_factor")
    @classmethod
    def _check_traffic_factor(cls, v: float) -> float:
        if not 1.0 <= v <= 3.0:
            raise ValueError("Traffic factor must be between 1.0 and 3.0.")
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
        return round(self.truck_volume_cuft * 10) - (1 if self.volume_strict else 0)

    @property
    def pallet_cap_tenths(self) -> int | None:
        if self.pallet_volume_cuft is None:
            return None
        return round(self.pallet_volume_cuft * 10) - (1 if self.volume_strict else 0)

    def load_cap_cuft(self, pallet: bool) -> float:
        """Volume limit of one load (the pallet limit when any stop needs a pallet)."""
        if pallet and self.pallet_volume_cuft is not None:
            return self.pallet_volume_cuft
        return self.truck_volume_cuft

    def volume_exceeds(self, volume: float, pallet: bool = False) -> bool:
        cap = self.load_cap_cuft(pallet)
        return volume >= cap - 1e-6 if self.volume_strict else volume > cap + 1e-6

    @property
    def first_load_seconds(self) -> int:
        return self.first_load_minutes * 60

    @property
    def floating(self) -> bool:
        """Driver start floats with the first stop (needs a first appointment time)."""
        return self.floating_start and self.earliest_delivery is not None

    @property
    def earliest_shift_start_s(self) -> int:
        return min(time_to_seconds(self.earliest_shift_start), self.shift_start_s)

    @property
    def model_origin_s(self) -> int:
        """Clock time of solver time 0: the earliest possible start when the start floats."""
        return self.earliest_shift_start_s if self.floating else self.shift_start_s

    @property
    def model_offset_s(self) -> int:
        """Seconds from solver time 0 to shift_start (0 unless the start floats)."""
        return self.shift_start_s - self.model_origin_s

    @property
    def earliest_delivery_rel_s(self) -> int:
        """First allowed arrival at a stop, seconds after shift start (0 = no limit)."""
        if self.earliest_delivery is None:
            return 0
        return max(0, time_to_seconds(self.earliest_delivery) - self.shift_start_s)

    @property
    def turn_gap_seconds(self) -> int:
        return self.turn_gap_minutes * 60

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

    def stop_penalty(self, tier: int, volume: float) -> int:
        """Drop penalty for one stop.  With a volume value, a D stop is worth one stop plus
        its volume / ``volume_value_cuft`` stops, so a planner short of trucks keeps both
        many stops and much volume."""
        base = self.penalty_for_tier(tier)
        weight, per = self.goal_weights
        if not per or tier > 3:
            return base
        # keep the tiers ordered: each tier up is multiplied by k = (full truck) / (empty
        # stop), so the smallest stop of a tier outweighs the largest stop of the next one
        k = (weight + self.truck_volume_cuft / per) / weight
        return int(base * (weight + max(0.0, volume) / per) * k ** (3 - tier))

    @property
    def goal_weights(self) -> tuple[float, float | None]:
        """(value of one stop, cu ft worth one more stop) for the goal."""
        if self.goal == "custom":
            return 1.0, self.volume_value_cuft
        return GOAL_WEIGHTS[self.goal]

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


# goal -> (value of one stop, cu ft worth one more stop)
GOAL_WEIGHTS: dict[str, tuple[float, float | None]] = {
    "both": (1.0, 100.0),  # 100 cu ft = one stop
    "stops": (1.0, 10_000.0),  # stops first; volume only breaks ties
    "volume": (0.05, 100.0),  # volume first; a stop alone is worth 5 cu ft
}


def goal_description(goal: GoalName) -> str:
    return {
        "both": "Stops + volume: each stop counts as 1 + its cu ft / 100 (300 cu ft = 4 stops).",
        "stops": "Max stops: as many stops as possible; volume only decides between equal counts.",
        "volume": "Max volume: as many cu ft as possible; small stops only when they fit on the way.",
        "custom": "Custom: each stop counts as 1 + its cu ft / Volume value (Advanced settings).",
    }[goal]


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
    "travel_provider": (
        "Auto uses road travel times from OSRM when it is running, otherwise straight-line "
        "estimates. TomTom uses truck times with predicted traffic (needs TOMTOM_API_KEY; "
        "falls back to OSRM)."
    ),
    "haversine_road_factor": (
        "Straight-line distance is multiplied by this to approximate road distance "
        "(estimates only)."
    ),
    "haversine_avg_speed_mph": "Average speed assumed for straight-line estimates (estimates only).",
    "profile": (
        "CA LFLT = the CA pilot rules (06:00 start, 800 / 400 cu ft, two turns, 200 / 300 mi). "
        "Legacy = the original single-hub brief."
    ),
    "first_load_minutes": "Loading time at the DC before the first departure of the day.",
    "floating_start": (
        "Driver start follows the first stop: start = first appointment - first loading - "
        "drive to the first stop, and the on-duty hours count from that start "
        "(e.g. 06:00-16:00, or 08:00-18:00)."
    ),
    "earliest_shift_start": (
        "Earliest a driver may start (the DC opens). A far first stop is then reached later "
        "than the first appointment: start + first loading + drive."
    ),
    "late_first_stop_cost": (
        "Cost per second a truck's first stop is reached after the first appointment "
        "(a far first stop): higher = prefer first stops reachable by the appointment."
    ),
    "earliest_delivery": "First customer appointment; a truck that arrives earlier waits.",
    "volume_strict": "Volume must stay below the cap (not equal to it).",
    "pallet_volume_cuft": "Volume limit of a load carrying any shipment that needs a pallet.",
    "unload_minutes_per_cuft": (
        "Unloading minutes per cubic foot: a stop's service time = its shipments' volume x this rate."
    ),
    "max_one_way_miles": "One-way limit: miles from the DC to a load's last stop.",
    "carrier_check_miles": "Loads farther than this one way are flagged: confirm with the carrier.",
    "two_turn_max_miles": "Total miles of all turns of a truck that runs a second turn.",
    "turn_gap_minutes": "A second turn leaves at least this long after the first turn's last delivery.",
    "obs_mode": (
        "Last = plan every other stop first, then add OBS-only stops just to raise the stop "
        "count, on loads that already pass within the OBS radius. Exclude = leave OBS-only "
        "stops out of the run. Normal = OBS stops compete like any other stop (tier 1)."
    ),
    "obs_radius_miles": (
        "An OBS stop is only added to a load that already visits a planned stop within this "
        "many miles (straight line)."
    ),
    "geocode_mode": (
        "Address = geocode every row from its street address and use the file's latitude/longitude "
        "only when the address cannot be matched. Fill = keep the file's coordinates and geocode "
        "only rows without them."
    ),
    "traffic_depart_time": (
        "TomTom: travel times are predicted for the delivery day at this time (historical "
        "traffic). A past date uses the same weekday in the coming weeks."
    ),
    "truck_dimensions": (
        "TomTom truck routing: length, height and gross weight (avoids roads the truck may "
        "not use)."
    ),
    "traffic_factor": (
        "OSRM road times assume empty roads; they are multiplied by this for daytime "
        "traffic (distances are not changed). Straight-line estimates are not scaled."
    ),
    "goal": (
        "Which stops to keep when there are more candidates than trucks: A = stops and "
        "volume together, B = most stops, C = most volume, Custom = Volume value."
    ),
    "use_priority": (
        "On: priority 1 (existing LFLT loads in the upload's priority column) is always "
        "planned before priority 2. Off: every candidate is alike and the plan keeps the "
        "stops with the most stops and volume."
    ),
    "volume_value_cuft": (
        "Used when Goal = Custom. Each stop counts as 1 + its volume / this many cu ft "
        "(100 = a 300 cu ft stop is worth 4 small stops). Raises both stops and volume when "
        "there are more candidates than trucks. 0 = count stops only."
    ),
    "stop_transit_minutes": (
        "Stops in transit: minutes added to every drive between two stops (not to the drive "
        "from or back to the DC). Unloading time is separate: stop volume x unloading rate."
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
