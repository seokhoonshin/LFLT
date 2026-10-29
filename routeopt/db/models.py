"""SQLAlchemy 2.x ORM models (brief section 11).

Only portable column types are used (String, Integer, Float, Boolean, Text, DateTime,
LargeBinary) so the same schema runs on SQLite, Postgres and SQL Server.  JSON documents
are stored as text.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    from routeopt.domain import hub_now  # local import keeps the ORM module dependency-light

    return hub_now()


class TruckRow(Base):
    __tablename__ = "trucks"

    truck_number: Mapped[str] = mapped_column(String(16), primary_key=True)
    driver_name: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str] = mapped_column(String(120), default="planner")
    plan_date: Mapped[dt.date] = mapped_column(Date)
    run_date: Mapped[dt.date] = mapped_column(Date)
    preset: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(64), default="")
    matrix_source: Mapped[str] = mapped_column(String(32), default="haversine")
    matrix_key: Mapped[str] = mapped_column(String(80), default="")
    settings_json: Mapped[str] = mapped_column(Text)
    solver_stats_json: Mapped[str] = mapped_column(Text, default="{}")
    kpis_json: Mapped[str] = mapped_column(Text, default="{}")
    source_name: Mapped[str] = mapped_column(String(260), default="")
    source_sha256: Mapped[str] = mapped_column(String(64), default="")
    source_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    stops_total: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class RunShipmentRow(Base):
    __tablename__ = "run_shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    shipment_id: Mapped[str] = mapped_column(String(64))
    source_row: Mapped[int] = mapped_column(Integer, default=0)
    shipment_json: Mapped[str] = mapped_column(Text)


class StopRow(Base):
    __tablename__ = "stops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    stop_id: Mapped[str] = mapped_column(String(16))
    node_index: Mapped[int] = mapped_column(Integer)
    in_scope: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_json: Mapped[str] = mapped_column(Text)


class RunTruckRow(Base):
    __tablename__ = "run_trucks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    truck_number: Mapped[str] = mapped_column(String(16))
    driver_name: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PlanVersionRow(Base):
    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str] = mapped_column(String(120), default="planner")
    note: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text)
    kpis_json: Mapped[str] = mapped_column(Text, default="{}")
    violations_json: Mapped[str] = mapped_column(Text, default="[]")
    export_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_plan_versions_run_version", "run_id", "version", unique=True),)


class LoadRow(Base):
    __tablename__ = "loads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    truck_number: Mapped[str] = mapped_column(String(16))
    driver_name: Mapped[str] = mapped_column(String(120), default="")
    seq: Mapped[int] = mapped_column(Integer)
    depart_s: Mapped[int] = mapped_column(Integer)
    return_s: Mapped[int] = mapped_column(Integer)
    load_out_volume: Mapped[float] = mapped_column(Float)
    max_volume_on_road: Mapped[float] = mapped_column(Float)
    drive_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    distance_miles: Mapped[float] = mapped_column(Float, default=0.0)


class LoadStopRow(Base):
    __tablename__ = "load_stops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    truck_number: Mapped[str] = mapped_column(String(16))
    load_seq: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    stop_id: Mapped[str] = mapped_column(String(16))
    arrive_s: Mapped[int] = mapped_column(Integer)
    depart_s: Mapped[int] = mapped_column(Integer)
    volume_after: Mapped[float] = mapped_column(Float)
    eta_window_start_s: Mapped[int] = mapped_column(Integer)
    eta_window_end_s: Mapped[int] = mapped_column(Integer)


class UnassignedRow(Base):
    __tablename__ = "unassigned"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    stop_id: Mapped[str] = mapped_column(String(16))
    tier: Mapped[int] = mapped_column(Integer)
    day_tag: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class EditLogRow(Base):
    __tablename__ = "edit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    who: Mapped[str] = mapped_column(String(120), default="planner")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    operation: Mapped[str] = mapped_column(String(32))
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")


class ExportRow(Base):
    __tablename__ = "exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.run_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TravelCacheRow(Base):
    __tablename__ = "travel_cache"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    matrix_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class GeocodeCacheRow(Base):
    __tablename__ = "geocode_cache"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    matched_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AppStateRow(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AppointmentRow(Base):
    """One shipment of a truck picked on Results for appointment requests (a snapshot of
    the plan at the time it was picked, plus the status the planner sets)."""

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    truck: Mapped[str] = mapped_column(String(64))
    load_seq: Mapped[int] = mapped_column(Integer)
    stop_seq: Mapped[int] = mapped_column(Integer)
    stop_id: Mapped[str] = mapped_column(String(32))
    shipment_id: Mapped[str] = mapped_column(String(128))
    customer: Mapped[str] = mapped_column(Text, default="")
    address: Mapped[str] = mapped_column(Text, default="")
    bill_to_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_date: Mapped[dt.date] = mapped_column(Date)
    eta: Mapped[str] = mapped_column(String(16), default="")
    window: Mapped[str] = mapped_column(String(40), default="")
    volume_cuft: Mapped[float] = mapped_column(Float, default=0.0)
    miles_from_prev: Mapped[float] = mapped_column(Float, default=0.0)
    stop_transit_min: Mapped[float] = mapped_column(Float, default=0.0)
    poc: Mapped[str] = mapped_column(Text, default="")
    dc: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="Not requested")
    note: Mapped[str] = mapped_column(Text, default="")
    selected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
