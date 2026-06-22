"""SQLAlchemy models for the PatchGuard ERP.

All geography columns are SRID 4326 (WGS84) — the same lat/lng every other component
speaks. Conversions to/from WKT live in geo.py only.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    inspector = "inspector"
    viewer = "viewer"


class Source(str, enum.Enum):
    worker = "worker"
    mobile = "mobile"


class InspectionStatus(str, enum.Enum):
    running = "running"
    done = "done"
    failed = "failed"


class ActionStatus(str, enum.Enum):
    open = "open"
    notified = "notified"
    resolved = "resolved"
    disputed = "disputed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Contractor(Base):
    __tablename__ = "contractors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    abn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    work_records: Mapped[list[WorkRecord]] = relationship(back_populates="contractor")


class WorkRecord(Base):
    __tablename__ = "work_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    contractor_id: Mapped[str] = mapped_column(ForeignKey("contractors.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    work_date: Mapped[date] = mapped_column(Date)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    hours_spent: Mapped[Decimal] = mapped_column(Numeric(7, 1))
    guarantee_months: Mapped[int] = mapped_column(Integer)
    guarantee_expires: Mapped[date] = mapped_column(Date, index=True)
    path = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=True)
    invoice_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    contractor: Mapped[Contractor] = relationship(back_populates="work_records")


class Inspection(Base):
    __tablename__ = "inspections"

    # id == the agent's job_id so worker uploads link without extra lookups
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    start_pt = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    end_pt = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    route = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=True)
    status: Mapped[InspectionStatus] = mapped_column(
        Enum(InspectionStatus, name="inspection_status"), default=InspectionStatus.running
    )
    captured: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    images: Mapped[list[Image]] = relationship(back_populates="inspection")


class Image(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    inspection_id: Mapped[str | None] = mapped_column(
        ForeignKey("inspections.id"), nullable=True, index=True
    )
    pt = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    lat: Mapped[float] = mapped_column(Float, index=True)
    lng: Mapped[float] = mapped_column(Float, index=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[str] = mapped_column(String(64))  # ISO-8601 as sent by worker
    raw_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    annotated_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    vision_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=Source.worker, server_default="worker")
    analyzed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    inspection: Mapped[Inspection | None] = relationship(back_populates="images")
    damages: Mapped[list[Damage]] = relationship(back_populates="image")


class Damage(Base):
    __tablename__ = "damages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    damage_class: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    bbox_x1: Mapped[int] = mapped_column(Integer)
    bbox_y1: Mapped[int] = mapped_column(Integer)
    bbox_x2: Mapped[int] = mapped_column(Integer)
    bbox_y2: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(64))

    image: Mapped[Image] = relationship(back_populates="damages")


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("image_id", "work_record_id", name="uq_action_image_work"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    damage_id: Mapped[str | None] = mapped_column(ForeignKey("damages.id"), nullable=True)
    work_record_id: Mapped[str] = mapped_column(ForeignKey("work_records.id"), index=True)
    contractor_id: Mapped[str] = mapped_column(ForeignKey("contractors.id"), index=True)
    distance_m: Mapped[Decimal] = mapped_column(Numeric(8, 1))
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus, name="action_status"), default=ActionStatus.open, index=True
    )
    auto_created: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    inspection_id: Mapped[str] = mapped_column(ForeignKey("inspections.id"), index=True)
    generated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True)
    content_md: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
