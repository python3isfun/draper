"""SQLite-backed repository: one file, no server.

Each object is stored as its Pydantic JSON document, with the columns needed for lookups
and ordering pulled out alongside it. Package revisions are append-only: saving an
existing (package_id, revision_id) raises, matching the in-memory store.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..models import Job, ProductionPackage, QAReport, Revision
from . import _fail_interrupted

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_assets (
    asset_id    TEXT PRIMARY KEY,
    meta        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE TABLE IF NOT EXISTS packages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- save order, newest = highest
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    published   INTEGER NOT NULL,
    data        TEXT NOT NULL,
    UNIQUE (package_id, revision_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_reports (
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, revision_id)
);
"""


class SqliteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the event loop and background tasks; the lock serializes
        # access because sqlite3 connections are not safe for concurrent use.
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has schema v{version}; this code supports v{SCHEMA_VERSION}"
                )
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> str | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # Source assets ------------------------------------------------------

    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO source_assets (asset_id, meta) VALUES (?, ?) "
            "ON CONFLICT (asset_id) DO UPDATE SET meta = excluded.meta",
            (asset_id, json.dumps(meta, ensure_ascii=False)),
        )

    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None:
        raw = self._one("SELECT meta FROM source_assets WHERE asset_id = ?", (asset_id,))
        return json.loads(raw) if raw else None

    # Jobs ---------------------------------------------------------------

    def save_job(self, job: Job) -> None:
        self._exec(
            "INSERT INTO jobs (job_id, status, created_at, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (job_id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job.job_id, job.status, job.created_at.isoformat(), job.model_dump_json()),
        )

    def get_job(self, job_id: str) -> Job | None:
        raw = self._one("SELECT data FROM jobs WHERE job_id = ?", (job_id,))
        return Job.model_validate_json(raw) if raw else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def fail_interrupted_jobs(self, reason: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
        return _fail_interrupted(
            [Job.model_validate_json(r[0]) for r in rows], reason, self.save_job
        )

    # Packages -----------------------------------------------------------

    def save_package(self, pkg: ProductionPackage, published: bool) -> None:
        try:
            self._exec(
                "INSERT INTO packages (package_id, revision_id, published, data) "
                "VALUES (?, ?, ?, ?)",
                (pkg.package_id, pkg.revision_id, int(published), pkg.model_dump_json()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"revision {pkg.revision_id} already stored; revisions are immutable"
            ) from exc

    def get_package(
        self, package_id: str, revision_id: str | None = None
    ) -> ProductionPackage | None:
        if revision_id:
            raw = self._one(
                "SELECT data FROM packages WHERE package_id = ? AND revision_id = ?",
                (package_id, revision_id),
            )
        else:
            # Latest published revision; a never-published package falls back to its
            # newest draft (same rule as the in-memory store).
            raw = self._one(
                "SELECT data FROM packages WHERE package_id = ? "
                "ORDER BY published DESC, seq DESC LIMIT 1",
                (package_id,),
            )
        return ProductionPackage.model_validate_json(raw) if raw else None

    # Revisions and evaluation reports -----------------------------------

    def save_revision(self, rev: Revision) -> None:
        self._exec(
            "INSERT INTO revisions (revision_id, package_id, data) VALUES (?, ?, ?) "
            "ON CONFLICT (revision_id) DO UPDATE SET data = excluded.data",
            (rev.revision_id, rev.package_id, rev.model_dump_json()),
        )

    def get_revision(self, revision_id: str) -> Revision | None:
        raw = self._one("SELECT data FROM revisions WHERE revision_id = ?", (revision_id,))
        return Revision.model_validate_json(raw) if raw else None

    def save_evaluation_report(self, package_id: str, revision_id: str, r: QAReport) -> None:
        self._exec(
            "INSERT INTO evaluation_reports (package_id, revision_id, data) VALUES (?, ?, ?) "
            "ON CONFLICT (package_id, revision_id) DO UPDATE SET data = excluded.data",
            (package_id, revision_id, r.model_dump_json()),
        )

    def get_evaluation_report(self, package_id: str, revision_id: str) -> QAReport | None:
        raw = self._one(
            "SELECT data FROM evaluation_reports WHERE package_id = ? AND revision_id = ?",
            (package_id, revision_id),
        )
        return QAReport.model_validate_json(raw) if raw else None
