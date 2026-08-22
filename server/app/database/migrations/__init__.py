"""Minimal, explicit schema migrations.

Alembic is overkill for a single-file SQLite database that one process owns.
Instead: a ``schema_version`` table plus an ordered list of migration steps.

Rules
-----
* A released migration is never edited — add a new one.
* Every step must be safe to run on a database that already contains user data
  (calibration and zones are irreplaceable field measurements).
* Fresh databases are created from the ORM metadata and then stamped with the
  latest version, so step 1 exists only for documentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    #: Executed in order inside one transaction.
    statements: list[str] = field(default_factory=list)


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="initial schema (settings, calibration_points, zones, presets, events)",
        statements=[],  # created from ORM metadata
    ),
    Migration(
        version=2,
        description="add detection capture review table",
        statements=[],  # new table is created from ORM metadata
    ),
    Migration(
        version=3,
        description="prevent deleted detection capture IDs from being reused",
        statements=[
            """
            CREATE TABLE detection_captures_v3 (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                ts DATETIME NOT NULL,
                camera_id VARCHAR(64) NOT NULL,
                "trigger" VARCHAR(32) NOT NULL,
                class_name VARCHAR(128) NOT NULL,
                confidence FLOAT,
                frame_seq INTEGER NOT NULL,
                frame_width INTEGER NOT NULL,
                frame_height INTEGER NOT NULL,
                model_name VARCHAR(256) NOT NULL,
                image_name VARCHAR(256) NOT NULL UNIQUE,
                detections JSON NOT NULL,
                settings JSON NOT NULL,
                review_status VARCHAR(32) NOT NULL,
                review_label VARCHAR(128) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """,
            """
            INSERT INTO detection_captures_v3 (
                id, ts, camera_id, "trigger", class_name, confidence, frame_seq,
                frame_width, frame_height, model_name, image_name, detections,
                settings, review_status, review_label, updated_at
            )
            SELECT
                id, ts, camera_id, "trigger", class_name, confidence, frame_seq,
                frame_width, frame_height, model_name, image_name, detections,
                settings, review_status, review_label, updated_at
            FROM detection_captures
            """,
            "DROP TABLE detection_captures",
            "ALTER TABLE detection_captures_v3 RENAME TO detection_captures",
            "CREATE INDEX ix_detection_captures_ts ON detection_captures (ts)",
            """
            CREATE INDEX ix_detection_captures_status_ts
            ON detection_captures (review_status, ts)
            """,
        ],
    ),
    # Example of what a future step looks like:
    # Migration(
    #     version=4,
    #     description="add calibration_points.confidence",
    #     statements=[
    #         "ALTER TABLE calibration_points ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
    #     ],
    # ),
]

LATEST_VERSION = max((m.version for m in MIGRATIONS), default=0)
