"""SQLAlchemy ORM models.

The database holds everything the user can change and everything worth keeping
across restarts: settings, calibration, zones, presets and the event history.
Transient state (current angles, tracks, frames) is never persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SettingsRecord(Base):
    """One row per settings section, storing the validated model as JSON.

    Keeping sections as JSON documents (instead of a column per field) means
    adding a setting never needs a schema migration — the Pydantic model in
    :mod:`app.services.settings_schema` fills in defaults for missing keys.
    """

    __tablename__ = "settings"

    section: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CalibrationPoint(Base):
    """A measured correspondence: camera pixel -> turret angles."""

    __tablename__ = "calibration_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, default="overview")
    #: Optional surface/zone label ("railing", "planter", "floor").
    surface: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: Normalised image coordinates in [0, 1] so calibration survives a
    #: resolution change on the camera.
    cam_x: Mapped[float] = mapped_column(Float, nullable=False)
    cam_y: Mapped[float] = mapped_column(Float, nullable=False)
    pan_deg: Mapped[float] = mapped_column(Float, nullable=False)
    tilt_deg: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "surface": self.surface,
            "label": self.label,
            "cam_x": self.cam_x,
            "cam_y": self.cam_y,
            "pan_deg": self.pan_deg,
            "tilt_deg": self.tilt_deg,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Zone(Base):
    """A polygon drawn on a camera image with a behavioural meaning."""

    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, default="overview")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: See app.targeting.zones.ZoneType
    zone_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: [[x, y], ...] in normalised image coordinates.
    points: Mapped[list[list[float]]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Higher priority wins when a point is inside several surface zones.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "name": self.name,
            "zone_type": self.zone_type,
            "points": self.points,
            "enabled": self.enabled,
            "priority": self.priority,
        }


class Preset(Base):
    """A named pan/tilt position for manual use."""

    __tablename__ = "presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    pan_deg: Mapped[float] = mapped_column(Float, nullable=False)
    tilt_deg: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "pan_deg": self.pan_deg,
            "tilt_deg": self.tilt_deg,
        }


class Event(Base):
    """Application event history shown in the UI and used for debugging."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "data": self.data,
            "snapshot": self.snapshot_path,
        }


Index("ix_events_category_ts", Event.category, Event.ts)


class DetectionCapture(Base):
    """A detector evidence frame that can later become training data."""

    __tablename__ = "detection_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="detection")
    class_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    frame_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frame_width: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_height: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    image_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    detections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unreviewed")
    review_label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "camera_id": self.camera_id,
            "trigger": self.trigger,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "frame_seq": self.frame_seq,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "model_name": self.model_name,
            "image_name": self.image_name,
            "detections": self.detections,
            "settings": self.settings,
            "review_status": self.review_status,
            "review_label": self.review_label,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


Index("ix_detection_captures_status_ts", DetectionCapture.review_status, DetectionCapture.ts)
