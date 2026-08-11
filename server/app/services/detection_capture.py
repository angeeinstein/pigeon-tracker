"""Persistent detector evidence and lightweight training-data review."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.camera.rtsp import encode_jpeg, safe_filename
from app.database.db import run_db
from app.database.models import DetectionCapture
from app.vision.detector import Detection

REVIEW_STATUSES = ("unreviewed", "training", "rejected")


class DetectionCaptureStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def create(
        self,
        *,
        image: np.ndarray,
        camera_id: str,
        frame_seq: int,
        detections: list[Detection],
        class_name: str,
        confidence: float | None,
        model_name: str,
        detector_settings: dict[str, object],
        jpeg_quality: int,
        trigger: str = "detection",
    ) -> dict[str, object]:
        """Encode one immutable source frame and persist its metadata."""
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        name = (
            f"{stamp}-{safe_filename(camera_id)}-{safe_filename(class_name or trigger)}-"
            f"{uuid4().hex[:8]}.jpg"
        )
        path = self.directory / name
        data = await asyncio.to_thread(encode_jpeg, image, jpeg_quality, None)
        await asyncio.to_thread(path.write_bytes, data)

        height, width = image.shape[:2]
        boxes = [d.as_dict() for d in detections]

        def _insert(session: Session) -> dict[str, object]:
            row = DetectionCapture(
                camera_id=camera_id,
                trigger=trigger,
                class_name=class_name,
                confidence=confidence,
                frame_seq=frame_seq,
                frame_width=int(width),
                frame_height=int(height),
                model_name=model_name,
                image_name=name,
                detections=boxes,
                settings=detector_settings,
            )
            session.add(row)
            session.flush()
            return row.as_dict()

        try:
            return await run_db(_insert)
        except Exception:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        review_status: str | None = None,
        class_name: str | None = None,
    ) -> list[dict[str, object]]:
        def _query(session: Session) -> list[dict[str, object]]:
            stmt = select(DetectionCapture).order_by(DetectionCapture.id.desc())
            if review_status:
                stmt = stmt.where(DetectionCapture.review_status == review_status)
            if class_name:
                stmt = stmt.where(DetectionCapture.class_name == class_name)
            stmt = stmt.limit(max(1, min(limit, 500))).offset(max(0, offset))
            return [row.as_dict() for row in session.scalars(stmt).all()]

        return await run_db(_query)

    async def update_review(
        self,
        capture_id: int,
        *,
        review_status: str,
        review_label: str,
    ) -> dict[str, object] | None:
        if review_status not in REVIEW_STATUSES:
            raise ValueError("invalid review status")

        def _update(session: Session) -> dict[str, object] | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            row.review_status = review_status
            row.review_label = review_label.strip()[:128]
            session.flush()
            session.refresh(row)
            return row.as_dict()

        return await run_db(_update)

    async def delete(self, capture_id: int) -> bool:
        def _delete(session: Session) -> str | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            name = row.image_name
            session.delete(row)
            return name

        name = await run_db(_delete)
        if name is None:
            return False
        await asyncio.to_thread((self.directory / name).unlink, missing_ok=True)
        return True

    def image_path(self, name: str) -> Path | None:
        """Resolve an image name without allowing traversal outside the store."""
        if Path(name).name != name:
            return None
        path = (self.directory / name).resolve()
        try:
            path.relative_to(self.directory.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    async def image_for(self, capture_id: int) -> Path | None:
        def _name(session: Session) -> str | None:
            row = session.get(DetectionCapture, capture_id)
            return row.image_name if row is not None else None

        name = await run_db(_name)
        return self.image_path(name) if name else None

    async def prune(self, retention_days: int, max_mb: int) -> int:
        """Remove old/unreviewed evidence while preserving training selections."""

        # The public list is capped for UI use; retention needs the complete set.
        def _all(session: Session) -> list[dict[str, object]]:
            stmt = select(DetectionCapture).order_by(DetectionCapture.id.asc())
            return [row.as_dict() for row in session.scalars(stmt).all()]

        rows = await run_db(_all)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0

        for row in list(rows):
            if row["review_status"] == "training":
                continue
            raw_ts = row.get("ts")
            timestamp = datetime.fromisoformat(str(raw_ts)) if raw_ts else cutoff
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp < cutoff and await self.delete(int(str(row["id"]))):
                rows.remove(row)
                removed += 1

        budget = max_mb * 1024 * 1024
        total = sum(
            path.stat().st_size
            for row in rows
            if (path := self.image_path(str(row["image_name"]))) is not None
        )
        for row in rows:
            if total <= budget:
                break
            if row["review_status"] == "training":
                continue
            path = self.image_path(str(row["image_name"]))
            size = path.stat().st_size if path is not None else 0
            if await self.delete(int(str(row["id"]))):
                total -= size
                removed += 1
        return removed
