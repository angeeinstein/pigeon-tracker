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
    # Example of what a future step looks like:
    # Migration(
    #     version=2,
    #     description="add calibration_points.confidence",
    #     statements=[
    #         "ALTER TABLE calibration_points ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
    #     ],
    # ),
]

LATEST_VERSION = max((m.version for m in MIGRATIONS), default=0)
