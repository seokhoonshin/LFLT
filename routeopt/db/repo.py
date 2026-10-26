"""Repository: all database access for RouteOpt (runs, versions, caches, settings).

Rules from the brief: v0 of a run is immutable once written; the original uploaded Excel
is kept alongside the run; deleting a run is soft-delete only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from routeopt.config import RunSettings
from routeopt.db.models import (
    AppStateRow,
    Base,
    EditLogRow,
    ExportRow,
    GeocodeCacheRow,
    LoadRow,
    LoadStopRow,
    PlanVersionRow,
    RunRow,
    RunShipmentRow,
    RunTruckRow,
    StopRow,
    TravelCacheRow,
    TruckRow,
    UnassignedRow,
)
from routeopt.domain import Plan, Shipment, Stop, TruckConfig, hub_now
from routeopt.ingest.validator import GeocodeResult
from routeopt.travel import node_coords, planning_matrix
from routeopt.travel.base import Matrix, coords_hash
from routeopt.travel.cache import TravelCache

DEFAULT_TRUCK_COUNT = 10


class EditLogEntry(BaseModel):
    """One manual-edit record (who, when, operation, before, after)."""

    who: str = "planner"
    at: dt.datetime = Field(default_factory=hub_now)
    operation: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None


@dataclass
class RunSummary:
    run_id: str
    created_at: dt.datetime
    plan_date: dt.date
    run_date: dt.date
    preset: str
    status: str
    matrix_source: str
    trucks_used: int
    trucks_selected: int
    stops_served: int
    stops_total: int
    unassigned: int
    version_count: int
    source_name: str
    deleted: bool


@dataclass
class ExportRecord:
    run_id: str
    version: int
    kind: str
    path: str
    created_at: dt.datetime
    override_reason: str | None


@dataclass
class RunRecord:
    run_id: str
    created_at: dt.datetime
    plan_date: dt.date
    run_date: dt.date
    preset: str
    status: str
    matrix_source: str
    settings: RunSettings
    stops: list[Stop]
    shipments: list[Shipment]
    matrix: Matrix
    trucks: list[TruckConfig]
    plans: dict[int, Plan]
    source_name: str
    source_bytes: bytes | None
    warnings: list[str]
    deleted: bool
    in_scope_ids: set[str] = field(default_factory=set)

    @property
    def latest_version(self) -> int:
        return max(self.plans) if self.plans else 0


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


class DbTravelCacheStore:
    """TravelCacheStore backed by the ``travel_cache`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def load(self, provider: str, key: str) -> Matrix | None:
        with self._sf() as session:
            row = session.get(TravelCacheRow, (provider, key))
            if row is None:
                return None
            return Matrix.model_validate_json(row.matrix_json)

    def save(self, provider: str, key: str, matrix: Matrix) -> None:
        with self._sf() as session, session.begin():
            row = session.get(TravelCacheRow, (provider, key))
            payload = matrix.model_dump_json()
            if row is None:
                session.add(
                    TravelCacheRow(provider=provider, key=key, n=matrix.n, matrix_json=payload)
                )
            else:
                row.matrix_json = payload
                row.n = matrix.n


class DbGeocodeCache:
    """GeocodeCache backed by the ``geocode_cache`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def get_many(self, keys: list[str]) -> dict[str, GeocodeResult]:
        if not keys:
            return {}
        with self._sf() as session:
            rows = session.execute(select(GeocodeCacheRow).where(GeocodeCacheRow.key.in_(keys)))
            return {
                r.key: GeocodeResult(
                    r.key, r.latitude, r.longitude, r.matched_address, r.match_type, from_cache=True
                )
                for r in rows.scalars()
            }

    def put_many(self, results: list[GeocodeResult]) -> None:
        with self._sf() as session, session.begin():
            for res in results:
                if not res.matched or res.latitude is None or res.longitude is None:
                    continue
                row = session.get(GeocodeCacheRow, res.key)
                if row is None:
                    session.add(
                        GeocodeCacheRow(
                            key=res.key,
                            latitude=res.latitude,
                            longitude=res.longitude,
                            matched_address=res.matched_address,
                            match_type=res.match_type,
                        )
                    )
                else:
                    row.latitude, row.longitude = res.latitude, res.longitude
                    row.matched_address, row.match_type = res.matched_address, res.match_type


class Repository:
    """Session-per-method data access layer."""

    def __init__(self, db_url: str, echo: bool = False) -> None:
        self.db_url = db_url
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            db_url, echo=echo, future=True, connect_args=connect_args
        )
        if db_url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _fk_on(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self._sf: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)

    # ---- schema ---------------------------------------------------------------------
    def init_schema(self) -> None:
        """Create all tables (idempotent)."""
        Base.metadata.create_all(self.engine)

    # ---- trucks ---------------------------------------------------------------------
    def list_trucks(self) -> list[TruckConfig]:
        rows = self._truck_rows()
        if not rows:
            with self._sf() as session, session.begin():
                for i in range(1, DEFAULT_TRUCK_COUNT + 1):
                    session.add(
                        TruckRow(
                            truck_number=f"T{i:02d}", driver_name="", active=True, sort_order=i
                        )
                    )
            rows = self._truck_rows()
        return rows

    def _truck_rows(self) -> list[TruckConfig]:
        with self._sf() as session:
            rows = session.execute(
                select(TruckRow).order_by(TruckRow.sort_order, TruckRow.truck_number)
            ).scalars()
            return [
                TruckConfig(truck_number=r.truck_number, driver_name=r.driver_name, active=r.active)
                for r in rows
            ]

    def save_trucks(self, trucks: list[TruckConfig]) -> None:
        """Full replace: upsert given trucks, delete the ones not listed."""
        with self._sf() as session, session.begin():
            keep = {t.truck_number for t in trucks}
            for existing in session.execute(select(TruckRow)).scalars():
                if existing.truck_number not in keep:
                    session.delete(existing)
            for i, t in enumerate(trucks, start=1):
                row = session.get(TruckRow, t.truck_number)
                if row is None:
                    session.add(
                        TruckRow(
                            truck_number=t.truck_number,
                            driver_name=t.driver_name,
                            active=t.active,
                            sort_order=i,
                        )
                    )
                else:
                    row.driver_name, row.active, row.sort_order = t.driver_name, t.active, i

    # ---- runs -----------------------------------------------------------------------
    def save_run(
        self,
        plan: Plan,
        stops: list[Stop],
        settings: RunSettings,
        matrix: Matrix,
        shipments: list[Shipment],
        trucks: list[TruckConfig],
        source_name: str = "",
        source_bytes: bytes | None = None,
        warnings: list[str] | None = None,
        in_scope_ids: set[str] | None = None,
        created_by: str = "planner",
    ) -> None:
        """Persist a new run with its version-0 plan (immutable afterwards)."""
        if plan.plan_version != 0:
            raise ValueError("A run must be saved with its optimizer plan (version 0).")
        in_scope = in_scope_ids if in_scope_ids is not None else {s.stop_id for s in stops}
        key = coords_hash(matrix.coords)
        with self._sf() as session, session.begin():
            if session.get(RunRow, plan.run_id) is not None:
                raise ValueError(f"Run {plan.run_id} already exists; version 0 is immutable.")
            session.add(
                RunRow(
                    run_id=plan.run_id,
                    created_at=plan.created_at,
                    created_by=created_by,
                    plan_date=plan.plan_date,
                    run_date=plan.plan_date - dt.timedelta(days=2),
                    preset=settings.preset,
                    status=plan.solver_stats.status,
                    matrix_source=plan.matrix_source,
                    matrix_key=key,
                    settings_json=_dumps(settings.model_dump(mode="json")),
                    solver_stats_json=plan.solver_stats.model_dump_json(),
                    kpis_json=plan.kpis.model_dump_json(),
                    source_name=source_name,
                    source_sha256=hashlib.sha256(source_bytes).hexdigest() if source_bytes else "",
                    source_bytes=source_bytes,
                    warnings_json=_dumps(list(warnings or [])),
                    stops_total=len(stops),
                )
            )
            session.flush()
            for s in shipments:
                session.add(
                    RunShipmentRow(
                        run_id=plan.run_id,
                        shipment_id=s.shipment_id,
                        source_row=s.source_row,
                        shipment_json=s.model_dump_json(),
                    )
                )
            for i, stop in enumerate(stops, start=1):
                session.add(
                    StopRow(
                        run_id=plan.run_id,
                        stop_id=stop.stop_id,
                        node_index=i,
                        in_scope=stop.stop_id in in_scope,
                        stop_json=stop.model_dump_json(),
                    )
                )
            for i, t in enumerate(trucks, start=1):
                session.add(
                    RunTruckRow(
                        run_id=plan.run_id,
                        truck_number=t.truck_number,
                        driver_name=t.driver_name,
                        active=t.active,
                        sort_order=i,
                    )
                )
            self._write_version(session, plan, created_by, "optimizer result")
        TravelCache(DbTravelCacheStore(self._sf)).put(matrix)

    def _write_version(self, session: Session, plan: Plan, created_by: str, note: str) -> None:
        session.add(
            PlanVersionRow(
                run_id=plan.run_id,
                version=plan.plan_version,
                created_at=plan.created_at,
                created_by=created_by,
                note=note,
                plan_json=plan.model_dump_json(),
                kpis_json=plan.kpis.model_dump_json(),
                violations_json=_dumps([v.model_dump() for v in plan.violations]),
                export_override_reason=plan.export_override_reason,
            )
        )
        for truck in plan.trucks:
            for load in truck.loads:
                if not load.stops:
                    continue
                session.add(
                    LoadRow(
                        run_id=plan.run_id,
                        version=plan.plan_version,
                        truck_number=truck.number,
                        driver_name=truck.driver_name,
                        seq=load.seq,
                        depart_s=load.depart_s,
                        return_s=load.return_s,
                        load_out_volume=load.load_out_volume,
                        max_volume_on_road=load.max_volume_on_road,
                        drive_minutes=load.drive_minutes,
                        distance_miles=load.distance_miles,
                    )
                )
                for visit in load.stops:
                    session.add(
                        LoadStopRow(
                            run_id=plan.run_id,
                            version=plan.plan_version,
                            truck_number=truck.number,
                            load_seq=load.seq,
                            seq=visit.seq,
                            stop_id=visit.stop_id,
                            arrive_s=visit.arrive_s,
                            depart_s=visit.depart_s,
                            volume_after=visit.volume_after,
                            eta_window_start_s=visit.eta_window_start_s,
                            eta_window_end_s=visit.eta_window_end_s,
                        )
                    )
        for u in plan.unassigned:
            session.add(
                UnassignedRow(
                    run_id=plan.run_id,
                    version=plan.plan_version,
                    stop_id=u.stop_id,
                    tier=u.tier,
                    day_tag=u.day_tag.value,
                    volume=u.volume,
                    reason=u.reason,
                    detail=u.detail,
                )
            )

    def list_runs(self, limit: int = 200, include_deleted: bool = False) -> list[RunSummary]:
        with self._sf() as session:
            stmt = select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
            if not include_deleted:
                stmt = stmt.where(RunRow.deleted.is_(False))
            out: list[RunSummary] = []
            for r in session.execute(stmt).scalars():
                kpis = json.loads(r.kpis_json or "{}")
                versions = (
                    session.execute(
                        select(PlanVersionRow.version).where(PlanVersionRow.run_id == r.run_id)
                    )
                    .scalars()
                    .all()
                )
                out.append(
                    RunSummary(
                        run_id=r.run_id,
                        created_at=r.created_at,
                        plan_date=r.plan_date,
                        run_date=r.run_date,
                        preset=r.preset,
                        status=r.status,
                        matrix_source=r.matrix_source,
                        trucks_used=int(kpis.get("trucks_used", 0)),
                        trucks_selected=int(kpis.get("trucks_selected", 0)),
                        stops_served=int(kpis.get("stops_served", 0)),
                        stops_total=r.stops_total,
                        unassigned=int(kpis.get("unassigned_stops", 0)),
                        version_count=len(versions),
                        source_name=r.source_name,
                        deleted=r.deleted,
                    )
                )
            return out

    def load_run(self, run_id: str) -> RunRecord:
        with self._sf() as session:
            r = session.get(RunRow, run_id)
            if r is None:
                raise LookupError(f"Run {run_id} was not found.")
            settings = RunSettings.model_validate(json.loads(r.settings_json))
            stop_rows = session.execute(
                select(StopRow).where(StopRow.run_id == run_id).order_by(StopRow.node_index)
            ).scalars()
            stops: list[Stop] = []
            in_scope: set[str] = set()
            for row in stop_rows:
                stop = Stop.model_validate_json(row.stop_json)
                stops.append(stop)
                if row.in_scope:
                    in_scope.add(stop.stop_id)
            shipments = [
                Shipment.model_validate_json(row.shipment_json)
                for row in session.execute(
                    select(RunShipmentRow)
                    .where(RunShipmentRow.run_id == run_id)
                    .order_by(RunShipmentRow.source_row)
                ).scalars()
            ]
            trucks = [
                TruckConfig(truck_number=t.truck_number, driver_name=t.driver_name, active=t.active)
                for t in session.execute(
                    select(RunTruckRow)
                    .where(RunTruckRow.run_id == run_id)
                    .order_by(RunTruckRow.sort_order)
                ).scalars()
            ]
            plans = {
                row.version: Plan.model_validate_json(row.plan_json)
                for row in session.execute(
                    select(PlanVersionRow).where(PlanVersionRow.run_id == run_id)
                ).scalars()
            }
            record_fields = {
                "run_id": r.run_id,
                "created_at": r.created_at,
                "plan_date": r.plan_date,
                "run_date": r.run_date,
                "preset": r.preset,
                "status": r.status,
                "matrix_source": r.matrix_source,
                "source_name": r.source_name,
                "source_bytes": r.source_bytes,
                "warnings": list(json.loads(r.warnings_json or "[]")),
                "deleted": r.deleted,
            }
        matrix = TravelCache(DbTravelCacheStore(self._sf)).get(
            str(record_fields["matrix_source"]), node_coords(stops)
        )
        if matrix is not None:  # the cache holds the raw matrix; plans use the adjusted one
            matrix = planning_matrix(matrix, settings)
        if matrix is None:
            raise LookupError(
                f"The travel matrix for run {run_id} is no longer in the cache; re-run the plan "
                "to recompute travel times."
            )
        return RunRecord(
            settings=settings,
            stops=stops,
            shipments=shipments,
            matrix=matrix,
            trucks=trucks,
            plans=plans,
            in_scope_ids=in_scope,
            **record_fields,  # type: ignore[arg-type]
        )

    def load_plan(self, run_id: str, version: int) -> Plan:
        with self._sf() as session:
            row = session.execute(
                select(PlanVersionRow).where(
                    PlanVersionRow.run_id == run_id, PlanVersionRow.version == version
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"Run {run_id} has no version {version}.")
            return Plan.model_validate_json(row.plan_json)

    def list_versions(self, run_id: str) -> list[int]:
        with self._sf() as session:
            return sorted(
                session.execute(
                    select(PlanVersionRow.version).where(PlanVersionRow.run_id == run_id)
                ).scalars()
            )

    def save_plan_version(
        self,
        run_id: str,
        plan: Plan,
        edits: list[EditLogEntry] | None = None,
        created_by: str = "planner",
        note: str = "",
    ) -> int:
        """Store a planner-edited version (must be newer than every existing version)."""
        if plan.run_id != run_id:
            raise ValueError("Plan does not belong to this run.")
        existing = self.list_versions(run_id)
        if not existing:
            raise LookupError(f"Run {run_id} was not found.")
        if plan.plan_version == 0 or plan.plan_version <= max(existing):
            raise ValueError(
                f"Version {plan.plan_version} cannot be written: versions are append-only "
                f"(next is v{max(existing) + 1}) and v0 is immutable."
            )
        with self._sf() as session, session.begin():
            self._write_version(session, plan, created_by, note or "planner edit")
            for e in edits or []:
                session.add(
                    EditLogRow(
                        run_id=run_id,
                        version=plan.plan_version,
                        who=e.who,
                        at=e.at,
                        operation=e.operation,
                        before_json=_dumps(e.before),
                        after_json=_dumps(e.after),
                    )
                )
        return plan.plan_version

    def log_edits(self, run_id: str, version: int, edits: list[EditLogEntry]) -> None:
        with self._sf() as session, session.begin():
            for e in edits:
                session.add(
                    EditLogRow(
                        run_id=run_id,
                        version=version,
                        who=e.who,
                        at=e.at,
                        operation=e.operation,
                        before_json=_dumps(e.before),
                        after_json=_dumps(e.after),
                    )
                )

    def list_edits(self, run_id: str) -> list[EditLogEntry]:
        with self._sf() as session:
            rows = session.execute(
                select(EditLogRow).where(EditLogRow.run_id == run_id).order_by(EditLogRow.id)
            ).scalars()
            return [
                EditLogEntry(
                    who=r.who,
                    at=r.at,
                    operation=r.operation,
                    before=json.loads(r.before_json or "{}"),
                    after=json.loads(r.after_json or "{}"),
                    version=r.version,
                )
                for r in rows
            ]

    def record_export(
        self, run_id: str, version: int, kind: str, path: str, override_reason: str | None = None
    ) -> None:
        with self._sf() as session, session.begin():
            session.add(
                ExportRow(
                    run_id=run_id,
                    version=version,
                    kind=kind,
                    path=str(path),
                    override_reason=override_reason,
                )
            )

    def list_exports(self, run_id: str) -> list[ExportRecord]:
        with self._sf() as session:
            rows = session.execute(
                select(ExportRow).where(ExportRow.run_id == run_id).order_by(ExportRow.id)
            ).scalars()
            return [
                ExportRecord(r.run_id, r.version, r.kind, r.path, r.created_at, r.override_reason)
                for r in rows
            ]

    def soft_delete_run(self, run_id: str) -> None:
        with self._sf() as session, session.begin():
            r = session.get(RunRow, run_id)
            if r is None:
                raise LookupError(f"Run {run_id} was not found.")
            r.deleted = True
            r.deleted_at = hub_now()

    def restore_run(self, run_id: str) -> None:
        with self._sf() as session, session.begin():
            r = session.get(RunRow, run_id)
            if r is None:
                raise LookupError(f"Run {run_id} was not found.")
            r.deleted = False
            r.deleted_at = None

    # ---- last-used settings -----------------------------------------------------------
    def load_last_settings(self) -> RunSettings | None:
        with self._sf() as session:
            row = session.get(AppStateRow, "last_settings")
            if row is None:
                return None
            try:
                return RunSettings.model_validate(json.loads(row.value_json))
            except ValueError:
                return None

    def save_last_settings(self, settings: RunSettings) -> None:
        with self._sf() as session, session.begin():
            row = session.get(AppStateRow, "last_settings")
            payload = _dumps(settings.model_dump(mode="json"))
            if row is None:
                session.add(AppStateRow(key="last_settings", value_json=payload))
            else:
                row.value_json = payload
                row.updated_at = hub_now()

    # ---- caches -----------------------------------------------------------------------
    def travel_cache_store(self) -> DbTravelCacheStore:
        return DbTravelCacheStore(self._sf)

    def travel_cache(self) -> TravelCache:
        return TravelCache(self.travel_cache_store())

    def geocode_cache(self) -> DbGeocodeCache:
        return DbGeocodeCache(self._sf)

    def clear_travel_cache(self) -> int:
        """Remove cached matrices not referenced by any run; returns the number removed."""
        with self._sf() as session, session.begin():
            referenced = set(session.execute(select(RunRow.matrix_key)).scalars())
            rows = list(session.execute(select(TravelCacheRow)).scalars())
            n = 0
            for row in rows:
                if row.key not in referenced:
                    session.delete(row)
                    n += 1
            return n

    def clear_geocode_cache(self) -> int:
        with self._sf() as session, session.begin():
            result = session.execute(delete(GeocodeCacheRow))
            return int(getattr(result, "rowcount", 0) or 0)
