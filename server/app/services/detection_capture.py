"""Persistent detector evidence and lightweight training-data review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from sqlalchemy import and_, func, not_, select
from sqlalchemy.orm import Session

from app.camera.rtsp import encode_jpeg, safe_filename
from app.database.db import run_db
from app.database.models import DetectionCapture
from app.vision.detector import Detection

REVIEW_STATUSES = ("unreviewed", "training", "rejected")
ANNOTATION_STATUSES = ("unreviewed", "accepted", "rejected")


def _review_queue_conditions(
    review_status: str | None,
    class_name: str | None,
) -> list[Any]:
    """Build filters for captures that represent model or manual evidence.

    Older releases stored raw foreground regions as ``motion`` annotations.
    They are model-input hints rather than object labels, so keep those legacy
    records on disk but out of every manual-review listing.
    """
    conditions = [
        not_(
            and_(
                DetectionCapture.trigger == "motion-rescan",
                DetectionCapture.class_name == "motion",
            )
        )
    ]
    if review_status:
        conditions.append(DetectionCapture.review_status == review_status)
    if class_name:
        conditions.append(DetectionCapture.class_name == class_name)
    return conditions


def _normalise_annotation(raw: dict[str, Any]) -> dict[str, Any]:
    """Add review fields to an old immutable detector proposal."""
    bbox = raw.get("bbox", [0.0, 0.0, 0.0, 0.0])
    return {
        "bbox": [float(value) for value in bbox[:4]],
        "confidence": (
            round(float(raw["confidence"]), 3) if raw.get("confidence") is not None else None
        ),
        "class_id": int(raw["class_id"]) if raw.get("class_id") is not None else None,
        "class_name": str(raw.get("class_name", "")),
        "source": str(raw.get("source", "proposal")),
        "review_status": str(raw.get("review_status", "unreviewed")),
        "review_label": str(raw.get("review_label", "")),
    }


def _sync_capture_review(row: DetectionCapture) -> None:
    annotations = [_normalise_annotation(item) for item in row.detections]
    row.detections = annotations
    if not annotations or any(item["review_status"] == "unreviewed" for item in annotations):
        row.review_status = "unreviewed"
        row.review_label = ""
        return
    accepted = [item for item in annotations if item["review_status"] == "accepted"]
    if accepted:
        row.review_status = "training"
        labels = sorted({str(item["review_label"] or "bird") for item in accepted})
        row.review_label = ", ".join(labels)
    else:
        row.review_status = "rejected"
        row.review_label = "not-bird"


def _capture_dict(row: DetectionCapture) -> dict[str, Any]:
    payload = row.as_dict()
    payload["detections"] = [_normalise_annotation(item) for item in row.detections]
    return payload


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
        boxes = [_normalise_annotation(d.as_dict()) for d in detections]

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
            return _capture_dict(row)

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
            conditions = _review_queue_conditions(review_status, class_name)
            stmt = (
                select(DetectionCapture)
                .where(*conditions)
                .order_by(DetectionCapture.id.desc())
            )
            stmt = stmt.limit(max(1, min(limit, 500))).offset(max(0, offset))
            return [_capture_dict(row) for row in session.scalars(stmt).all()]

        return await run_db(_query)

    async def page(
        self,
        *,
        limit: int = 60,
        offset: int = 0,
        review_status: str | None = None,
        class_name: str | None = None,
    ) -> dict[str, object]:
        """Return one metadata page plus the uncapped filtered total."""

        def _query(session: Session) -> dict[str, object]:
            conditions = _review_queue_conditions(review_status, class_name)
            total = int(
                session.scalar(
                    select(func.count()).select_from(DetectionCapture).where(*conditions)
                )
                or 0
            )
            stmt = (
                select(DetectionCapture)
                .where(*conditions)
                .order_by(DetectionCapture.id.desc())
                .limit(max(1, min(limit, 200)))
                .offset(max(0, offset))
            )
            return {
                "items": [_capture_dict(row) for row in session.scalars(stmt).all()],
                "total": total,
                "offset": max(0, offset),
                "limit": max(1, min(limit, 200)),
            }

        return await run_db(_query)

    async def navigate(
        self,
        capture_id: int,
        *,
        direction: str,
        review_status: str | None = None,
        class_name: str | None = None,
    ) -> dict[str, object] | None:
        """Find an adjacent filtered capture by id, independent of page boundaries."""
        if direction not in {"current", "previous", "next"}:
            raise ValueError("invalid navigation direction")

        def _query(session: Session) -> dict[str, object] | None:
            conditions = _review_queue_conditions(review_status, class_name)

            if direction == "current":
                # The current capture may just have left the active filter
                # because the user completed its review. Return it once more
                # so the client can refresh its state and then navigate from
                # its id to the next item that still matches the queue.
                target = session.get(DetectionCapture, capture_id)
            elif direction == "next":
                target = session.scalar(
                    select(DetectionCapture)
                    .where(*conditions, DetectionCapture.id < capture_id)
                    .order_by(DetectionCapture.id.desc())
                    .limit(1)
                )
            else:
                target = session.scalar(
                    select(DetectionCapture)
                    .where(*conditions, DetectionCapture.id > capture_id)
                    .order_by(DetectionCapture.id.asc())
                    .limit(1)
                )
            if target is None:
                return None

            total = int(
                session.scalar(
                    select(func.count()).select_from(DetectionCapture).where(*conditions)
                )
                or 0
            )
            newer = int(
                session.scalar(
                    select(func.count())
                    .select_from(DetectionCapture)
                    .where(*conditions, DetectionCapture.id > target.id)
                )
                or 0
            )
            older = int(
                session.scalar(
                    select(func.count())
                    .select_from(DetectionCapture)
                    .where(*conditions, DetectionCapture.id < target.id)
                )
                or 0
            )
            return {
                "capture": _capture_dict(target),
                "position": min(newer + 1, total) if total else 0,
                "total": total,
                "has_previous": newer > 0,
                "has_next": older > 0,
            }

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
            annotations = [_normalise_annotation(item) for item in row.detections]
            if review_status in {"unreviewed", "rejected"}:
                annotation_status = review_status
                for annotation in annotations:
                    annotation["review_status"] = annotation_status
                    annotation["review_label"] = ""
                row.detections = annotations
            row.review_status = review_status
            row.review_label = review_label.strip()[:128]
            session.flush()
            session.refresh(row)
            return _capture_dict(row)

        return await run_db(_update)

    async def review_annotation(
        self,
        capture_id: int,
        annotation_index: int,
        *,
        review_status: str,
        review_label: str = "",
    ) -> dict[str, object] | None:
        if review_status not in ANNOTATION_STATUSES:
            raise ValueError("invalid annotation review status")

        def _update(session: Session) -> dict[str, object] | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            annotations = [_normalise_annotation(item) for item in row.detections]
            if annotation_index < 0 or annotation_index >= len(annotations):
                raise IndexError("annotation not found")
            annotation = annotations[annotation_index]
            annotation["review_status"] = review_status
            annotation["review_label"] = (
                review_label.strip()[:128] if review_status == "accepted" else ""
            )
            row.detections = annotations
            _sync_capture_review(row)
            session.flush()
            session.refresh(row)
            return _capture_dict(row)

        return await run_db(_update)

    async def add_annotation(
        self,
        capture_id: int,
        *,
        bbox: Sequence[float],
        class_name: str,
    ) -> dict[str, object] | None:
        def _add(session: Session) -> dict[str, object] | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            x1, y1, x2, y2 = bbox
            x1 = max(0.0, min(float(row.frame_width), x1))
            x2 = max(0.0, min(float(row.frame_width), x2))
            y1 = max(0.0, min(float(row.frame_height), y1))
            y2 = max(0.0, min(float(row.frame_height), y2))
            if x2 - x1 < 2.0 or y2 - y1 < 2.0:
                raise ValueError("annotation box is too small")
            annotations = [_normalise_annotation(item) for item in row.detections]
            annotations.append(
                {
                    "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "confidence": None,
                    "class_id": None,
                    "class_name": class_name.strip()[:128] or "bird",
                    "source": "manual",
                    "review_status": "accepted",
                    "review_label": class_name.strip()[:128] or "bird",
                }
            )
            row.detections = annotations
            _sync_capture_review(row)
            session.flush()
            session.refresh(row)
            return _capture_dict(row)

        return await run_db(_add)

    async def delete_annotation(
        self, capture_id: int, annotation_index: int
    ) -> dict[str, object] | None:
        def _delete(session: Session) -> dict[str, object] | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            annotations = [_normalise_annotation(item) for item in row.detections]
            if annotation_index < 0 or annotation_index >= len(annotations):
                raise IndexError("annotation not found")
            if annotations[annotation_index]["source"] != "manual":
                raise ValueError("model proposals cannot be deleted; reject them instead")
            annotations.pop(annotation_index)
            row.detections = annotations
            _sync_capture_review(row)
            session.flush()
            session.refresh(row)
            return _capture_dict(row)

        return await run_db(_delete)

    async def reject_unreviewed_annotations(self, capture_id: int) -> dict[str, object] | None:
        def _reject(session: Session) -> dict[str, object] | None:
            row = session.get(DetectionCapture, capture_id)
            if row is None:
                return None
            annotations = [_normalise_annotation(item) for item in row.detections]
            for annotation in annotations:
                if annotation["review_status"] == "unreviewed":
                    annotation["review_status"] = "rejected"
                    annotation["review_label"] = ""
            row.detections = annotations
            _sync_capture_review(row)
            session.flush()
            session.refresh(row)
            return _capture_dict(row)

        return await run_db(_reject)

    async def export_yolo(self) -> tuple[Path, dict[str, int]]:
        """Build a YOLO dataset from fully reviewed captures.

        Accepted annotations become ``bird`` boxes. Fully rejected frames are
        exported with empty label files as useful negative examples. Original
        model proposals and review decisions are retained in ``manifest.json``.
        """

        def _reviewed(session: Session) -> list[dict[str, Any]]:
            stmt = select(DetectionCapture).where(
                DetectionCapture.review_status.in_(("training", "rejected"))
            )
            return [_capture_dict(row) for row in session.scalars(stmt).all()]

        rows = await run_db(_reviewed)
        return await asyncio.to_thread(self._write_yolo_zip, rows)

    def _write_yolo_zip(self, rows: Sequence[dict[str, Any]]) -> tuple[Path, dict[str, int]]:
        usable: list[dict[str, Any]] = []
        for row in rows:
            annotations = [_normalise_annotation(item) for item in row["detections"]]
            accepted = [item for item in annotations if item["review_status"] == "accepted"]
            if accepted or row["review_status"] == "rejected":
                row = dict(row)
                row["detections"] = annotations
                row["accepted_annotations"] = accepted
                usable.append(row)
        if not usable:
            raise ValueError("no fully reviewed captures are available for export")

        splits = _episode_splits(usable)
        fd, raw_path = tempfile.mkstemp(prefix="pigeon-dataset-", suffix=".zip")
        os.close(fd)
        path = Path(raw_path)
        manifest: list[dict[str, Any]] = []
        positives = 0
        negatives = 0
        has_independent_validation = "val" in splits.values()
        try:
            # JPEGs are already compressed; storing them avoids wasting server CPU during export.
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                for row in usable:
                    image_path = self.image_path(str(row["image_name"]))
                    if image_path is None:
                        continue
                    capture_id = int(row["id"])
                    split = splits[capture_id]
                    stem = f"capture-{capture_id:08d}"
                    archive.write(image_path, f"pigeon-dataset/images/{split}/{stem}.jpg")
                    labels = []
                    for annotation in row["accepted_annotations"]:
                        yolo = _yolo_box(
                            annotation["bbox"], int(row["frame_width"]), int(row["frame_height"])
                        )
                        if yolo is not None:
                            labels.append("0 " + " ".join(f"{value:.6f}" for value in yolo))
                    archive.writestr(
                        f"pigeon-dataset/labels/{split}/{stem}.txt",
                        "\n".join(labels) + ("\n" if labels else ""),
                    )
                    positives += bool(labels)
                    negatives += not bool(labels)
                    manifest.append(
                        {
                            "capture_id": capture_id,
                            "timestamp": row["ts"],
                            "camera_id": row["camera_id"],
                            "split": split,
                            "source_image": row["image_name"],
                            "frame_width": row["frame_width"],
                            "frame_height": row["frame_height"],
                            "trigger": row["trigger"],
                            "model": row["model_name"],
                            "detector_settings": row["settings"],
                            "original_proposals": row["detections"],
                            "accepted_boxes": row["accepted_annotations"],
                        }
                    )
                archive.writestr(
                    "pigeon-dataset/dataset.yaml",
                    "path: .\n"
                    "train: images/train\n"
                    f"val: images/{'val' if has_independent_validation else 'train'}\n"
                    "names:\n  0: bird\n",
                )
                archive.writestr(
                    "pigeon-dataset/manifest.json",
                    json.dumps(
                        {
                            "format_version": 1,
                            "class_names": ["bird"],
                            "split_strategy": (
                                "camera episodes kept together"
                                if has_independent_validation
                                else "train reused for validation; collect another episode"
                            ),
                            "captures": manifest,
                        },
                        indent=2,
                        default=str,
                    ),
                )
                archive.writestr(
                    "pigeon-dataset/README.txt",
                    "YOLO detection dataset exported by pigeon-tracker.\n"
                    "Only individually accepted boxes are labels. Rejected frames are included "
                    "as empty negative examples.\n"
                    "On Windows, double-click train_windows.bat. It creates a reusable Python "
                    "environment, installs the required training packages, validates this "
                    "dataset and opens a training GUI. The trained best.pt is written below "
                    "the training-runs directory.\n"
                    "Train/validation assignment keeps captures from the same camera episode "
                    "together. "
                    + (
                        "Inspect manifest.json before training.\n"
                        if has_independent_validation
                        else "Only one episode was available, so dataset.yaml reuses the training "
                        "set for validation. Those metrics are not independent; collect another "
                        "episode before evaluating a model.\n"
                    ),
                )
                training_assets = Path(__file__).resolve().with_name("training_assets")
                for asset_name in ("train_model.py", "train_windows.bat"):
                    archive.write(
                        training_assets / asset_name,
                        f"pigeon-dataset/{asset_name}",
                    )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path, {
            "images": positives + negatives,
            "positive_images": positives,
            "negative_images": negatives,
            "boxes": sum(len(row["accepted_annotations"]) for row in usable),
        }

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
            return [_capture_dict(row) for row in session.scalars(stmt).all()]

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


def _yolo_box(
    bbox: list[float], frame_width: int, frame_height: int
) -> tuple[float, float, float, float] | None:
    if frame_width <= 0 or frame_height <= 0:
        return None
    x1, y1, x2, y2 = bbox
    x1 = max(0.0, min(float(frame_width), float(x1)))
    x2 = max(0.0, min(float(frame_width), float(x2)))
    y1 = max(0.0, min(float(frame_height), float(y1)))
    y2 = max(0.0, min(float(frame_height), float(y2)))
    if x2 <= x1 or y2 <= y1:
        return None
    return (
        ((x1 + x2) / 2.0) / frame_width,
        ((y1 + y2) / 2.0) / frame_height,
        (x2 - x1) / frame_width,
        (y2 - y1) / frame_height,
    )


def _episode_splits(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Keep temporally adjacent frames together to reduce validation leakage."""
    ordered = sorted(rows, key=lambda row: (str(row["camera_id"]), str(row["ts"])))
    episodes: list[list[int]] = []
    last_camera = ""
    last_ts: datetime | None = None
    for row in ordered:
        timestamp = datetime.fromisoformat(str(row["ts"]))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        camera = str(row["camera_id"])
        if camera != last_camera or last_ts is None or (timestamp - last_ts).total_seconds() > 60:
            episodes.append([])
        episodes[-1].append(int(row["id"]))
        last_camera = camera
        last_ts = timestamp

    validation: set[int] = set()
    if len(episodes) > 1:
        ranked = sorted(
            episodes,
            key=lambda episode: hashlib.sha256(str(episode[0]).encode()).hexdigest(),
        )
        count = max(1, round(len(ranked) * 0.2))
        validation = {capture_id for episode in ranked[:count] for capture_id in episode}
    return {int(row["id"]): "val" if int(row["id"]) in validation else "train" for row in rows}
