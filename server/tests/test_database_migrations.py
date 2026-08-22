"""Database upgrades preserve evidence while fixing public-ID guarantees."""

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text

from app.database.db import run_migrations
from app.database.migrations import LATEST_VERSION


def test_capture_autoincrement_migration_preserves_rows(tmp_path: Path) -> None:
    database = tmp_path / "version-2.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (2);
        CREATE TABLE detection_captures (
            id INTEGER NOT NULL PRIMARY KEY,
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
        );
        INSERT INTO detection_captures VALUES (
            7, '2026-08-22 12:00:00', 'overview', 'manual', 'manual', NULL,
            1, 160, 120, 'test.pt', 'capture-7.jpg', '[]', '{}',
            'unreviewed', '', '2026-08-22 12:00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{database}", future=True)
    assert run_migrations(engine) == LATEST_VERSION
    with engine.begin() as migrated:
        table_sql = migrated.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'detection_captures'"
            )
        ).scalar_one()
        assert "AUTOINCREMENT" in table_sql
        assert (
            migrated.execute(
                text("SELECT image_name FROM detection_captures WHERE id = 7")
            ).scalar_one()
            == "capture-7.jpg"
        )
        migrated.execute(text("DELETE FROM detection_captures WHERE id = 7"))
        migrated.execute(
            text(
                """
                INSERT INTO detection_captures (
                    ts, camera_id, "trigger", class_name, confidence, frame_seq,
                    frame_width, frame_height, model_name, image_name, detections,
                    settings, review_status, review_label, updated_at
                ) VALUES (
                    '2026-08-22 12:01:00', 'overview', 'manual', 'manual', NULL,
                    2, 160, 120, 'test.pt', 'capture-next.jpg', '[]', '{}',
                    'unreviewed', '', '2026-08-22 12:01:00'
                )
                """
            )
        )
        assert (
            migrated.execute(
                text("SELECT id FROM detection_captures WHERE image_name = 'capture-next.jpg'")
            ).scalar_one()
            > 7
        )
    engine.dispose()
