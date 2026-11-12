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

from ..models import Job, LLMCall, ProductionPackage, QAReport, Revision, TraceEvent
from ..models.images import GeneratedImage
from ..models.package import utcnow

# v1: objects. v2: job_queue. v3: trace tables + job_queue.pause_requested.
# v4: generated_images. Each step is additive, so older files upgrade in place (see _migrate).
SCHEMA_VERSION = 4

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
CREATE TABLE IF NOT EXISTS job_queue (
    job_id           TEXT PRIMARY KEY,
    state            TEXT NOT NULL,              -- queued | leased | done | dead
    attempts         INTEGER NOT NULL DEFAULT 0,
    available_at     REAL NOT NULL,              -- unix seconds; retry backoff
    enqueued_at      REAL NOT NULL,
    lease_owner      TEXT,
    lease_expires_at REAL,
    last_error       TEXT,
    checkpoint       TEXT,                       -- PipelineCheckpoint JSON
    pause_requested  INTEGER NOT NULL DEFAULT 0  -- user asked to pause at the next safe point
);
CREATE INDEX IF NOT EXISTS job_queue_ready ON job_queue (state, available_at);
CREATE TABLE IF NOT EXISTS job_events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    data    TEXT NOT NULL                    -- TraceEvent JSON (without seq)
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id, seq);
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id           TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    meta              TEXT NOT NULL,         -- LLMCall JSON without the bodies below
    request           TEXT NOT NULL DEFAULT '{}',
    commentary        TEXT NOT NULL DEFAULT '',
    reasoning_summary TEXT NOT NULL DEFAULT '',
    output            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS llm_calls_job ON llm_calls (job_id, started_at);
CREATE TABLE IF NOT EXISTS generated_images (
    image_id    TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    status      TEXT NOT NULL,              -- pending | running | done | failed
    created_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS generated_images_pkg ON generated_images (package_id, created_at);
CREATE INDEX IF NOT EXISTS generated_images_status ON generated_images (status, created_at);
"""


# Jobs saved before the `archived` field existed have no key: not archived.
_NOT_ARCHIVED = "coalesce(json_extract(data, '$.archived'), 0) = 0"


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
            # Wait for other processes (e.g. a separate worker) instead of failing at once.
            self._conn.execute("PRAGMA busy_timeout=5000")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has schema v{version}; this code supports v{SCHEMA_VERSION}"
                )
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """Column additions that CREATE TABLE IF NOT EXISTS can't apply to old tables."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(job_queue)")}
        if "pause_requested" not in cols:
            self._conn.execute(
                "ALTER TABLE job_queue ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
            )

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

    def get_source_assets(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Several assets in one query (e.g. file names for the job list)."""
        ids = list(dict.fromkeys(asset_ids))[:500]  # stays under SQLite's variable limit
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT asset_id, meta FROM source_assets WHERE asset_id IN ({marks})",
                tuple(ids),
            ).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

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

    def list_jobs(self, limit: int = 50, include_archived: bool = True) -> list[Job]:
        where = "" if include_archived else f"WHERE {_NOT_ARCHIVED} "
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM jobs {where}ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def jobs_for_package(self, package_id: str) -> list[Job]:
        """Jobs that made this package, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM jobs WHERE json_extract(data, '$.package_id') = ? "
                "ORDER BY created_at DESC",
                (package_id,),
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def list_archive_candidates(self) -> list[Job]:
        """Jobs not archived whose language isn't en-US (see archive.py)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM jobs WHERE {_NOT_ARCHIVED} AND "
                "coalesce(json_extract(data, '$.request.options.language'), '') != 'en-US'"
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def list_active_jobs(self) -> list[Job]:
        """Jobs not yet finished (queued or running), oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

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

    # Trace ----------------------------------------------------------------

    def add_event(self, event: TraceEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO job_events (job_id, data) VALUES (?, ?)",
                (event.job_id, event.model_dump_json(exclude={"seq"})),
            )
        return int(cur.lastrowid)

    def list_events(self, job_id: str, after_seq: int = 0, limit: int = 1000) -> list[TraceEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, data FROM job_events WHERE job_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (job_id, after_seq, limit),
            ).fetchall()
        return [TraceEvent.model_validate_json(d).model_copy(update={"seq": q}) for q, d in rows]

    _BODIES = ("request", "commentary", "reasoning_summary", "output")

    def save_llm_call(self, call: LLMCall) -> None:
        meta = call.model_dump_json(exclude=set(self._BODIES))
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_calls (call_id, job_id, started_at, meta, request, commentary, "
                "reasoning_summary, output) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (call_id) DO UPDATE SET meta = excluded.meta, "
                "request = excluded.request, commentary = excluded.commentary, "
                "reasoning_summary = excluded.reasoning_summary, output = excluded.output",
                (
                    call.call_id,
                    call.job_id,
                    call.started_at.isoformat(),
                    meta,
                    json.dumps(call.request, ensure_ascii=False),
                    call.commentary,
                    call.reasoning_summary,
                    call.output,
                ),
            )

    def get_llm_call(self, call_id: str) -> LLMCall | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT meta, request, commentary, reasoning_summary, output FROM llm_calls "
                "WHERE call_id = ?",
                (call_id,),
            ).fetchone()
        if row is None:
            return None
        meta, request, commentary, reasoning, output = row
        return LLMCall.model_validate_json(meta).model_copy(
            update={
                "request": json.loads(request),
                "commentary": commentary,
                "reasoning_summary": reasoning,
                "output": output,
            }
        )

    def list_llm_calls(self, job_id: str) -> list[LLMCall]:
        """Calls in start order, with commentary/reasoning but without request/output bodies
        (fetch one call with get_llm_call for those)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT meta, commentary, reasoning_summary, length(output) FROM llm_calls "
                "WHERE job_id = ? ORDER BY started_at, call_id",
                (job_id,),
            ).fetchall()
        return [
            LLMCall.model_validate_json(meta).model_copy(
                update={"commentary": c, "reasoning_summary": r, "output_chars": n}
            )
            for meta, c, r, n in rows
        ]

    # Generated images -------------------------------------------------------

    def save_image(self, image: GeneratedImage) -> None:
        self._exec(
            "INSERT INTO generated_images (image_id, package_id, revision_id, status, created_at, "
            "data) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (image_id) DO UPDATE SET "
            "status = excluded.status, data = excluded.data",
            (
                image.image_id,
                image.package_id,
                image.revision_id,
                image.status,
                image.created_at.isoformat(),
                image.model_dump_json(),
            ),
        )

    def get_image(self, image_id: str) -> GeneratedImage | None:
        raw = self._one("SELECT data FROM generated_images WHERE image_id = ?", (image_id,))
        return GeneratedImage.model_validate_json(raw) if raw else None

    def list_images(self, package_id: str, revision_id: str | None = None) -> list[GeneratedImage]:
        sql = "SELECT data FROM generated_images WHERE package_id = ?"
        params: tuple[Any, ...] = (package_id,)
        if revision_id is not None:
            sql += " AND revision_id = ?"
            params += (revision_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, image_id", params).fetchall()
        return [GeneratedImage.model_validate_json(r[0]) for r in rows]

    def claim_next_image(self) -> GeneratedImage | None:
        """The oldest pending image, marked running, atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM generated_images WHERE status = 'pending' "
                    "ORDER BY created_at, image_id LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                img = GeneratedImage.model_validate_json(row[0])
                img.status, img.started_at = "running", utcnow()
                self._conn.execute(
                    "UPDATE generated_images SET status = ?, data = ? WHERE image_id = ?",
                    (img.status, img.model_dump_json(), img.image_id),
                )
                self._conn.execute("COMMIT")
                return img
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def requeue_running_images(self, older_than_s: float = 0.0) -> int:
        """Queue again images left running for longer than older_than_s (interrupted).
        Several processes may share the database, so a younger running image belongs to
        someone still working on it."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM generated_images WHERE status = 'running'"
            ).fetchall()
        cutoff = utcnow().timestamp() - older_than_s
        n = 0
        for (raw,) in rows:
            img = GeneratedImage.model_validate_json(raw)
            if img.started_at and img.started_at.timestamp() > cutoff:
                continue
            img.status, img.started_at = "pending", None
            self.save_image(img)
            n += 1
        return n
