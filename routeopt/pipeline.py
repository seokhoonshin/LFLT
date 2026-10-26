"""End-to-end run pipeline (brief section 5), independent of the UI.

``Excel -> validate -> geocode-fill -> consolidate -> nodes -> matrix -> solve ->
extract -> validate plan -> (persist) -> render/export``.

:func:`optimize` covers the solver part (stops + matrix -> validated Plan) and is what the
UI runs in a worker process; :func:`run_file` covers the whole thing for the CLI.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import logging
import multiprocessing as mp
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from routeopt.config import AppConfig, RunSettings
from routeopt.domain import Plan, Stop, TruckConfig, UnassignedStop
from routeopt.ingest.consolidate import ConsolidationResult, consolidate
from routeopt.ingest.excel_loader import load_rows
from routeopt.ingest.validator import Geocoder, ValidationResult, validate_rows
from routeopt.logging_utils import get_logger
from routeopt.solver.extract import extract_plan, new_run_id
from routeopt.solver.model_builder import SolverInputs
from routeopt.solver.obs_addon import (
    REASON_OBS_EXCLUDED,
    add_obs_stops,
    is_obs_stop,
    obs_unassigned,
)
from routeopt.solver.plan_validator import validate_plan
from routeopt.solver.solve import solve
from routeopt.solver.unassigned_reasons import attribute_reasons
from routeopt.travel import FALLBACK_WARNING, ProviderChoice, node_coords, select_provider
from routeopt.travel.base import Matrix
from routeopt.travel.cache import TravelCache

log = logging.getLogger(__name__)

DEFAULT_TRUCKS = [
    TruckConfig(truck_number=f"T{i:02d}", driver_name=f"Driver {i}") for i in range(1, 11)
]


def default_trucks() -> list[TruckConfig]:
    return [t.model_copy() for t in DEFAULT_TRUCKS]


def select_trucks(settings: RunSettings, trucks: list[TruckConfig]) -> list[TruckConfig]:
    """Trucks for the run: the selected numbers, else all active."""
    active = [t for t in trucks if t.active]
    if settings.trucks_selected:
        wanted = set(settings.trucks_selected)
        return [t for t in active if t.truck_number in wanted]
    return active


def scope_stops(
    stops: list[Stop], matrix: Matrix, settings: RunSettings
) -> tuple[list[Stop], Matrix, list[UnassignedStop]]:
    """Keep D stops plus the enabled pull-forward days; restrict the matrix to match."""
    enabled = set(settings.pull_forward_days)
    keep: list[int] = [0]
    kept: list[Stop] = []
    excluded: list[UnassignedStop] = []
    for i, s in enumerate(stops):
        if s.day_tag.offset == 0 or s.day_tag.offset in enabled:
            keep.append(i + 1)
            kept.append(s)
        else:
            excluded.append(
                UnassignedStop(
                    stop_id=s.stop_id,
                    tier=s.tier,
                    day_tag=s.day_tag,
                    volume=s.delivery_volume,
                    pickup_volume=s.pickup_volume,
                    service_minutes=s.service_total_minutes,
                    reason=f"Not included: {s.day_tag.value} pull-forward is off",
                )
            )
    if len(kept) == len(stops):
        return stops, matrix, []
    return kept, matrix.submatrix(keep), excluded


@dataclass
class OptimizeResult:
    """Outcome of :func:`optimize`.

    ``stops``/``matrix`` are the in-scope stops (node order) and their matrix - what the
    plan, validator and editor work on.  ``all_stops``/``full_matrix`` cover every stop of
    the upload (pull-forward days that were switched off included) for persistence.
    """

    plan: Plan
    matrix: Matrix
    stops: list[Stop]
    anomalies: list[str] = field(default_factory=list)
    all_stops: list[Stop] = field(default_factory=list)
    full_matrix: Matrix | None = None

    @property
    def in_scope_ids(self) -> set[str]:
        return {s.stop_id for s in self.stops}


def optimize(
    stops: list[Stop],
    matrix: Matrix,
    settings: RunSettings,
    trucks: list[TruckConfig],
    run_id: str | None = None,
) -> OptimizeResult:
    """Solve, extract, validate and attribute reasons.  Never hangs: the solver is
    time-limited and a partial plan (or a clear failure message) always comes back."""
    run_id = run_id or new_run_id()
    rlog = get_logger(__name__, run_id)
    all_stops, full_matrix = stops, matrix
    stops, matrix, out_of_scope = scope_stops(stops, matrix, settings)
    # OBS-only stops are left out of the solve unless they compete normally
    obs_stops = [s for s in stops if is_obs_stop(s)] if settings.obs_mode != "normal" else []
    obs_ids = {s.stop_id for s in obs_stops}
    keep = [0] + [i + 1 for i, s in enumerate(stops) if s.stop_id not in obs_ids]
    solve_stops = [s for s in stops if s.stop_id not in obs_ids]
    solve_matrix = matrix.submatrix(keep) if obs_ids else matrix
    inputs = SolverInputs(stops=solve_stops, matrix=solve_matrix, settings=settings, trucks=trucks)
    t0 = time.perf_counter()
    outcome = solve(inputs)
    plan = extract_plan(outcome, inputs, run_id=run_id, matrix_source=matrix.source)
    plan.out_of_scope = out_of_scope
    if matrix.source != "osrm":
        plan.warnings.insert(0, FALLBACK_WARNING)
    plan.unassigned, anomalies = attribute_reasons(plan, solve_stops, settings, solve_matrix)
    if obs_stops and settings.obs_mode == "exclude":
        plan.out_of_scope += [obs_unassigned(s, REASON_OBS_EXCLUDED) for s in obs_stops]
        stops, matrix = solve_stops, solve_matrix
    elif obs_stops:
        plan = add_obs_stops(plan, stops, obs_stops, settings, matrix)
    plan.violations = validate_plan(plan, stops, settings, matrix)
    for a in anomalies:
        plan.warnings.append(f"Priority anomaly: {a}")
    rlog.info(
        "optimize done",
        extra={
            "x_status": plan.solver_stats.status,
            "x_trucks_used": plan.kpis.trucks_used,
            "x_stops_served": plan.kpis.stops_served,
            "x_unassigned": plan.kpis.unassigned_stops,
            "x_violations": len(plan.violations),
            "x_seconds": round(time.perf_counter() - t0, 1),
        },
    )
    return OptimizeResult(
        plan=plan,
        matrix=matrix,
        stops=stops,
        anomalies=anomalies,
        all_stops=all_stops,
        full_matrix=full_matrix,
    )


# --------------------------------------------------------------------------------------
# Ingest + travel helpers used by both CLI and UI
# --------------------------------------------------------------------------------------


@dataclass
class IngestResult:
    validation: ValidationResult
    consolidation: ConsolidationResult | None
    mapping_missing: list[str]
    headers: list[str]
    source_name: str


def ingest(
    source: str | Path | bytes | io.BytesIO,
    settings: RunSettings,
    geocoder: Geocoder | None = None,
    mapping: dict[str, str] | None = None,
    exclude_rows: set[int] | None = None,
) -> IngestResult:
    """Load -> validate -> geocode-fill -> consolidate."""
    table, column_mapping, rows = load_rows(source, mapping)
    if not column_mapping.complete:
        empty = ValidationResult(shipments=[], rows_read=len(rows))
        return IngestResult(
            empty, None, column_mapping.missing_required, table.headers, table.source_name
        )
    validation = validate_rows(
        rows, settings.plan_date, settings, geocoder=geocoder, exclude_rows=exclude_rows
    )
    consolidation = consolidate(validation.shipments, settings) if validation.ok else None
    if consolidation:
        validation.warnings.extend(consolidation.warnings)
    return IngestResult(validation, consolidation, [], table.headers, table.source_name)


def build_matrix(stops: list[Stop], choice: ProviderChoice) -> Matrix:
    return choice.provider.matrix(node_coords(stops))


def run_file(
    source: str | Path,
    settings: RunSettings,
    app: AppConfig | None = None,
    trucks: list[TruckConfig] | None = None,
    geocoder: Geocoder | None = None,
    cache: TravelCache | None = None,
) -> tuple[OptimizeResult, IngestResult, ProviderChoice]:
    """Whole pipeline for one workbook (CLI / tests)."""
    app = app or AppConfig()
    ing = ingest(source, settings, geocoder=geocoder)
    if ing.mapping_missing:
        raise ValueError(f"Missing required columns: {', '.join(ing.mapping_missing)}")
    if not ing.validation.ok:
        problems = "; ".join(f"Row {e.row}: {e.message}" for e in ing.validation.errors[:10])
        raise ValueError(f"The workbook has errors: {problems}")
    assert ing.consolidation is not None
    stops = ing.consolidation.stops
    choice = select_provider(settings, app, cache=cache)
    matrix = build_matrix(stops, choice)
    result = optimize(stops, matrix, settings, select_trucks(settings, trucks or default_trucks()))
    return result, ing, choice


# --------------------------------------------------------------------------------------
# Background job (worker process) so the UI stays responsive
# --------------------------------------------------------------------------------------


def _worker(
    conn: Any,
    stops: list[Stop],
    matrix: Matrix,
    settings: RunSettings,
    trucks: list[TruckConfig],
    run_id: str,
) -> None:
    try:
        result = optimize(stops, matrix, settings, trucks, run_id=run_id)
        conn.send(("ok", result))
    except Exception as exc:  # pragma: no cover - surfaced to the UI
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


@contextlib.contextmanager
def _neutral_main_module() -> Iterator[None]:
    """Keep a spawned child from re-executing the parent's ``__main__`` script.

    Streamlit runs each page as ``__main__`` with a ``__file__``; multiprocessing's spawn
    would re-run that page in the worker.  The worker only needs importable modules, so
    the main module is swapped for an empty one while the process starts.
    """
    import types

    original = sys.modules.get("__main__")
    dummy = types.ModuleType("__main__")
    sys.modules["__main__"] = dummy
    try:
        yield
    finally:
        if original is not None:
            sys.modules["__main__"] = original


class OptimizeJob:
    """Runs :func:`optimize` in a separate process (OR-Tools holds the GIL while solving).

    Falls back to a thread if a process cannot be started.  ``poll()`` returns the result
    when done; ``elapsed`` gives seconds since start; ``cancel()`` kills the worker.
    """

    def __init__(
        self,
        stops: list[Stop],
        matrix: Matrix,
        settings: RunSettings,
        trucks: list[TruckConfig],
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.started_at = time.perf_counter()
        self.result: OptimizeResult | None = None
        self.error: str | None = None
        self._done = False
        self._process: Any = None
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self._args = (stops, matrix, settings, trucks, self.run_id)
        try:
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe(duplex=False)
            self._conn = parent
            self._process = ctx.Process(target=_worker, args=(child, *self._args), daemon=True)
            with _neutral_main_module():
                self._process.start()
            child.close()
        except Exception as exc:  # pragma: no cover - platform dependent
            log.warning("could not start worker process (%s); using a thread", exc)
            self._process = None
            self._thread = threading.Thread(target=self._run_inline, daemon=True)
            self._thread.start()

    def _run_inline(self) -> None:
        try:
            self.result = optimize(*self._args)
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"
        self._done = True

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    @property
    def done(self) -> bool:
        self.poll()
        return self._done

    def poll(self, timeout: float = 0.0) -> OptimizeResult | None:
        if self._done:
            return self.result
        if self._process is not None:
            if self._conn.poll(timeout):
                try:
                    status, payload = self._conn.recv()
                except (EOFError, OSError):
                    status, payload = "error", "The optimizer process stopped before returning a plan."
                if status == "ok":
                    self.result = payload
                else:
                    self.error = str(payload)
                self._done = True
                self._process.join(timeout=1.0)
            elif not self._process.is_alive():
                self.error = "The optimizer process stopped unexpectedly."
                self._done = True
        elif self._thread is not None and not self._thread.is_alive():
            self._done = True
        return self.result

    def wait(self, timeout: float | None = None) -> OptimizeResult | None:
        deadline = None if timeout is None else time.perf_counter() + timeout
        while not self.done:
            remaining = (
                0.25 if deadline is None else min(0.25, max(0.0, deadline - time.perf_counter()))
            )
            self.poll(remaining)
            if deadline is not None and time.perf_counter() >= deadline:
                break
        return self.result

    def cancel(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self.error = "Cancelled by the planner."
            self._done = True


def today_d_minus_2(plan_date: dt.date) -> dt.date:
    return plan_date - dt.timedelta(days=2)
