"""Sequential timeline / volume engine shared by extraction, the validator and the editor.

Given the *structure* of a truck's day (ordered stop ids per load, where lunch goes) this
module rebuilds every clock time and every volume reading with plain arithmetic:

* the driver starts at ``shift_start`` - or, when the start floats (CA), at the first
  appointment minus the first loading minus the drive to the first stop - then travels;
  service + buffer at each stop; ``reload_minutes`` between loads; lunch either during a
  reload or as an explicit gap on one arc;
* load-out = sum of delivery volume of the load's stops; after each stop: - LMD + RTN.

All times are seconds since midnight of D.  This is deliberately independent of OR-Tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from routeopt.config import RunSettings
from routeopt.domain import (
    DEFAULT_SITE,
    N_DEPOTS,
    SITE_NODE,
    Load,
    LunchInfo,
    PlanTruck,
    Stop,
    StopVisit,
    clock,
    round_to_quarter,
)
from routeopt.travel.base import Matrix

HUB_NODE = 0
NOON_S = 12 * 3600


def site_node(site: str) -> int:
    """Matrix node of a loading site (depot nodes come first, see ``node_coords``)."""
    return SITE_NODE.get(site, HUB_NODE)


def load_site(stop_ids: list[str], stops_by_id: dict[str, Stop]) -> str:
    """A load leaves from the site of its stops (the validator flags mixed loads)."""
    return stops_by_id[stop_ids[0]].site if stop_ids else DEFAULT_SITE


def turn_departure(
    t: int,
    load_idx: int,
    prev_site: str | None,
    site: str,
    last_delivery: int | None,
    matrix: Matrix,
    settings: RunSettings,
) -> tuple[int, int | None, int]:
    """(departure, reload start, deadhead seconds) of load ``load_idx`` given ``t``, the time
    the truck is back at ``prev_site`` (or the shift start for the first load).

    Load 1 leaves after the first-load loading time.  A later load first drives DC-to-DC
    when it loads at another site, then reloads, and never leaves before the previous
    turn's last delivery + the turn gap.
    """
    if load_idx == 1:
        return t + settings.first_load_seconds, None, 0
    deadhead = 0
    if prev_site is not None and prev_site != site:
        deadhead = matrix.duration(site_node(prev_site), site_node(site))
    reload_start = t + deadhead
    depart = reload_start + settings.reload_seconds
    if last_delivery is not None and settings.turn_gap_seconds:
        depart = max(depart, last_delivery + settings.turn_gap_seconds)
    return depart, reload_start, deadhead


def truck_start(
    loads: list[list[str]],
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> int:
    """When the driver starts (seconds since midnight).

    Fixed at ``shift_start`` unless the start floats: then the truck reaches its first stop
    at the first appointment time, i.e. start = appointment - first loading - drive to the
    first stop, never before ``earliest_shift_start`` (a far first stop is then reached
    later).
    """
    if not settings.floating or not loads or not loads[0]:
        return settings.shift_start_s
    first = loads[0][0]
    depot = site_node(load_site(loads[0], stops_by_id))
    drive = matrix.duration(depot, node_of[first])
    appointment = settings.shift_start_s + settings.earliest_delivery_rel_s
    return max(settings.earliest_shift_start_s, appointment - settings.first_load_seconds - drive)


def wait_for_appointment(arrive: int, settings: RunSettings) -> int:
    """A truck that arrives before the first appointment time waits."""
    rel = settings.earliest_delivery_rel_s
    return max(arrive, settings.shift_start_s + rel) if rel else arrive


@dataclass
class RouteSkeleton:
    """The structural description of one truck's day."""

    truck_number: str
    driver_name: str = ""
    loads: list[list[str]] = field(default_factory=list)  # stop ids per load, in visit order
    lunch: LunchInfo | None = None  # None -> placed by rule
    solver_arrivals: dict[str, int] | None = None  # stop_id -> solver arrival (since midnight)
    solver_returns: list[int] | None = None  # per load, solver return time (since midnight)


def node_index_map(stops: list[Stop]) -> dict[str, int]:
    """stop_id -> matrix node (sites are nodes 0..N_DEPOTS-1, then stops in list order)."""
    return {s.stop_id: i + N_DEPOTS for i, s in enumerate(stops)}


def lunch_via_reload(settings: RunSettings) -> bool:
    """Whether a reload is long enough to host the lunch break."""
    return settings.reload_seconds >= settings.lunch_seconds


def choose_lunch_arc(
    skeleton_loads: list[list[str]],
    departs: list[list[int]],
    settings: RunSettings,
    travels: list[list[int]] | None = None,
) -> tuple[int, int, int]:
    """Pick (load_seq, after_stop_seq, requested_start) for an explicit lunch.

    ``departs[l][k]`` is the departure time from the k-th point of load l where index 0 is
    the hub departure and index k>=1 is the departure from stop k; ``travels[l][k]`` is
    the travel time of the arc that starts there.  Preference order:

    1. an arc departing inside the lunch window (lunch starts on departure, no waiting);
    2. an arc whose travel spans the window start (lunch interrupts the drive, no waiting);
    3. the arc closest before the window (the driver waits for the window to open);
    4. the earliest arc after the window (lunch starts late; the validator warns).
    """
    earliest_rel, latest_rel = settings.lunch_window_rel()
    if settings.lunch_earliest is None and settings.lunch_latest is None:
        earliest = NOON_S  # "any time": aim for noon
        latest = settings.shift_start_s + latest_rel
    else:
        earliest = settings.shift_start_s + earliest_rel
        latest = settings.shift_start_s + latest_rel
    best: tuple[tuple[int, int], tuple[int, int, int]] | None = None
    for load_idx, load_departs in enumerate(departs):
        for k, d in enumerate(load_departs):
            travel = travels[load_idx][k] if travels else 0
            if earliest <= d <= latest:
                score = (0, d - earliest)
                start = d
            elif d < earliest <= d + travel:
                score = (1, earliest - d)
                start = earliest
            elif d + travel < earliest:
                score = (2, earliest - (d + travel))
                start = earliest
            else:
                score = (3, d - latest)
                start = d
            if best is None or score < best[0]:
                best = (score, (load_idx + 1, k, start))
    assert best is not None
    return best[1]


def eta_window(arrive_s: int, settings: RunSettings) -> tuple[int, int]:
    """ETA +/- eta_window_hours, each end rounded to 15 min, lower end clipped to shift start."""
    w = settings.eta_window_seconds
    start = round_to_quarter(arrive_s - w)
    end = round_to_quarter(arrive_s + w)
    start = max(start, settings.shift_start_s)
    return start, max(end, start)


def build_truck(
    skeleton: RouteSkeleton,
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> PlanTruck:
    """Rebuild a truck's complete timeline, volumes and lunch from its skeleton."""
    loads = [list(load) for load in skeleton.loads if load]
    if not loads:
        return PlanTruck(number=skeleton.truck_number, driver_name=skeleton.driver_name)

    lunch = skeleton.lunch
    n_loads = len(loads)
    use_reload_lunch = n_loads >= 2 and lunch_via_reload(settings)
    if lunch is None or lunch.kind == "none":
        lunch = LunchInfo(kind="reload") if use_reload_lunch else LunchInfo(kind="break")
    if lunch.kind == "reload" and not use_reload_lunch:
        lunch = LunchInfo(kind="break")

    # First pass without an explicit lunch to know departure times (for rule placement).
    if lunch.kind == "break" and (lunch.load_seq is None or lunch.after_stop_seq is None):
        departs, travels = _departures_without_lunch(loads, stops_by_id, matrix, node_of, settings)
        load_seq, after_stop_seq, start = choose_lunch_arc(loads, departs, settings, travels)
        lunch = LunchInfo(
            kind="break", load_seq=load_seq, after_stop_seq=after_stop_seq, start_s=start
        )

    lunch_s = settings.lunch_seconds
    plan_loads: list[Load] = []
    start_s = truck_start(loads, stops_by_id, matrix, node_of, settings)
    t = start_s
    lunch_start_actual: int | None = None
    lunch_end_actual: int | None = None
    lunch_where = ""
    prev_site: str | None = None
    last_delivery: int | None = None
    for load_idx, stop_ids in enumerate(loads, start=1):
        stops = [stops_by_id[sid] for sid in stop_ids]
        site = load_site(stop_ids, stops_by_id)
        depot = site_node(site)
        load_out_vol = sum(s.delivery_volume for s in stops)
        load_out_wt = sum(s.delivery_weight for s in stops)
        load_out_pcs = sum(s.pieces_in for s in stops)
        depart, reload_start, deadhead = turn_departure(
            t, load_idx, prev_site, site, last_delivery, matrix, settings
        )
        if load_idx >= 2:
            t = reload_start if reload_start is not None else t
            if (
                lunch.kind == "reload"
                and lunch.load_seq in (None, load_idx)
                and lunch_start_actual is None
            ):
                # eat during the reload, as late as the lunch window asks if it still fits
                earliest_rel, _latest_rel = settings.lunch_window_rel()
                preferred = settings.shift_start_s + earliest_rel
                lunch_start_actual = t
                if t < preferred <= t + settings.reload_seconds - lunch_s:
                    lunch_start_actual = preferred
                lunch_end_actual = lunch_start_actual + lunch_s
                lunch_where = f"during the reload before load {load_idx}"
                lunch = lunch.model_copy(update={"load_seq": load_idx})
        t = depart
        vol, wt, pcs = load_out_vol, load_out_wt, load_out_pcs
        max_vol = vol
        prev_node = depot
        drive_s = 0
        dist_m = 0
        visits: list[StopVisit] = []
        lunch_on_return = False
        for k, stop in enumerate(stops, start=1):
            node = node_of[stop.stop_id]
            travel = matrix.duration(prev_node, node)
            dist = matrix.distance(prev_node, node)
            lunch_here = (
                lunch.kind == "break"
                and lunch.load_seq == load_idx
                and lunch.after_stop_seq == k - 1
            )
            arrive = t + travel
            if lunch_here:
                requested = lunch.start_s if lunch.start_s is not None else t
                lunch_start_actual = max(requested, t)
                lunch_end_actual = lunch_start_actual + lunch_s
                arrive = max(t + travel, lunch_start_actual) + lunch_s
                lunch_where = (
                    f"before the first stop of load {load_idx}"
                    if k == 1
                    else f"after stop {k - 1} of load {load_idx}"
                )
            arrive = wait_for_appointment(arrive, settings)
            depart_stop = arrive + stop.service_total_minutes * 60
            vol_arr, wt_arr, pcs_arr = vol, wt, pcs
            vol = vol - stop.delivery_volume + stop.pickup_volume
            wt = wt - stop.delivery_weight + stop.pickup_weight
            pcs = pcs - stop.pieces_in + stop.pieces_out
            max_vol = max(max_vol, vol_arr, vol)
            w_start, w_end = eta_window(arrive, settings)
            solver_arrive = (
                skeleton.solver_arrivals.get(stop.stop_id) if skeleton.solver_arrivals else None
            )
            visits.append(
                StopVisit(
                    seq=k,
                    stop_id=stop.stop_id,
                    arrive_s=arrive,
                    depart_s=depart_stop,
                    travel_min_from_prev=round(travel / 60, 1),
                    dist_mi_from_prev=round(dist / 1609.344, 1),
                    volume_on_arrival=round(vol_arr, 1),
                    volume_after=round(vol, 1),
                    weight_on_arrival=round(wt_arr, 1),
                    weight_after=round(wt, 1),
                    pieces_on_arrival=pcs_arr,
                    pieces_after=pcs,
                    eta_window_start_s=w_start,
                    eta_window_end_s=w_end,
                    lunch_before=lunch_here,
                    solver_arrive_s=solver_arrive,
                )
            )
            drive_s += travel
            dist_m += dist
            t = depart_stop
            prev_node = node
        last_delivery = t
        one_way_m = dist_m
        # back to the load's site
        travel = matrix.duration(prev_node, depot)
        dist = matrix.distance(prev_node, depot)
        lunch_here = (
            lunch.kind == "break"
            and lunch.load_seq == load_idx
            and lunch.after_stop_seq == len(stops)
        )
        arrive_hub = t + travel
        if lunch_here:
            requested = lunch.start_s if lunch.start_s is not None else t
            lunch_start_actual = max(requested, t)
            lunch_end_actual = lunch_start_actual + lunch_s
            arrive_hub = max(t + travel, lunch_start_actual) + lunch_s
            lunch_on_return = True
            lunch_where = f"after the last stop of load {load_idx}, before returning"
        drive_s += travel
        dist_m += dist
        solver_return = None
        if skeleton.solver_returns and load_idx - 1 < len(skeleton.solver_returns):
            solver_return = skeleton.solver_returns[load_idx - 1]
        plan_loads.append(
            Load(
                seq=load_idx,
                depart_s=depart,
                return_s=arrive_hub,
                load_out_volume=round(load_out_vol, 1),
                max_volume_on_road=round(max_vol, 1),
                load_out_weight=round(load_out_wt, 1),
                load_out_pieces=load_out_pcs,
                drive_minutes=round(drive_s / 60, 1),
                distance_miles=round(dist_m / 1609.344, 1),
                reload_start_s=reload_start,
                lunch_on_return=lunch_on_return,
                stops=visits,
                solver_return_s=solver_return,
                site=site,
                pallet=any(s.pallet for s in stops),
                deadhead_minutes=round(deadhead / 60, 1),
                one_way_miles=round(one_way_m / 1609.344, 1),
                last_delivery_s=last_delivery,
            )
        )
        t = arrive_hub
        prev_site = site

    lunch_info = LunchInfo(
        kind=lunch.kind,
        where=lunch_where or ("none" if lunch.kind == "none" else ""),
        load_seq=lunch.load_seq,
        after_stop_seq=lunch.after_stop_seq if lunch.kind == "break" else None,
        start_s=lunch_start_actual,
        end_s=lunch_end_actual,
    )
    if lunch_info.start_s is not None and lunch_info.end_s is not None:
        lunch_info.where = (
            f"{lunch_info.where} ({clock(lunch_info.start_s)}-{clock(lunch_info.end_s)})"
        )
    on_duty = (plan_loads[-1].return_s - start_s) / 60
    return PlanTruck(
        number=skeleton.truck_number,
        driver_name=skeleton.driver_name,
        on_duty_minutes=round(on_duty, 1),
        start_s=start_s,
        lunch=lunch_info,
        loads=plan_loads,
    )


def _departures_without_lunch(
    loads: list[list[str]],
    stops_by_id: dict[str, Stop],
    matrix: Matrix,
    node_of: dict[str, int],
    settings: RunSettings,
) -> tuple[list[list[int]], list[list[int]]]:
    """Per load: departure times (hub, then each stop) and the travel time of each arc."""
    departs_out: list[list[int]] = []
    travels_out: list[list[int]] = []
    t = truck_start(loads, stops_by_id, matrix, node_of, settings)
    prev_site: str | None = None
    last_delivery: int | None = None
    for load_idx, stop_ids in enumerate(loads, start=1):
        site = load_site(stop_ids, stops_by_id)
        depot = site_node(site)
        t, _reload_start, _deadhead = turn_departure(
            t, load_idx, prev_site, site, last_delivery, matrix, settings
        )
        departs = [t]
        travels: list[int] = []
        prev = depot
        for sid in stop_ids:
            node = node_of[sid]
            travel = matrix.duration(prev, node)
            travels.append(travel)
            t = wait_for_appointment(t + travel, settings)
            t += stops_by_id[sid].service_total_minutes * 60
            departs.append(t)
            prev = node
        last_delivery = t
        travels.append(matrix.duration(prev, depot))
        t += matrix.duration(prev, depot)
        departs_out.append(departs)
        travels_out.append(travels)
        prev_site = site
    return departs_out, travels_out


def skeleton_from_truck(truck: PlanTruck) -> RouteSkeleton:
    """Structural view of an existing PlanTruck (for re-validation and editing)."""
    return RouteSkeleton(
        truck_number=truck.number,
        driver_name=truck.driver_name,
        loads=[[v.stop_id for v in load.stops] for load in truck.loads if load.stops],
        lunch=truck.lunch,
        solver_arrivals={
            v.stop_id: v.solver_arrive_s
            for load in truck.loads
            for v in load.stops
            if v.solver_arrive_s is not None
        }
        or None,
        solver_returns=[int(load.solver_return_s or 0) for load in truck.loads]
        if truck.loads and all(load.solver_return_s is not None for load in truck.loads)
        else None,
    )
