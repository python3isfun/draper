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

from ..experiment import ExpSession, ExpTurn
from ..library.models import AssetImage, Casting, LibraryAsset
from ..models import Job, LLMCall, ProductionPackage, QAReport, Revision, TraceEvent
from ..models.images import DesignOption, GeneratedImage
from ..models.package import utcnow
from ..shot_frames import PromptVersion, ShotPlan, ShotPlanVersion, ShotTake, approval_changes
from ..shot_reading import SceneReading
from ..shots.models import ShotVideoPrompt

# v1: objects. v2: job_queue. v3: trace tables + job_queue.pause_requested.
# v4: generated_images. v5: design_options. v6: exp_sessions, exp_turns.
# v7: exp_sessions.next_label. v8: shot_takes. v9: shot_plans. v10: shot_prompts.
# v11: scene_readings. v12: the asset library (library_assets, story_casting, asset_images).
# v13: shot_plan_versions (story_settings came in at v12, without a bump).
# v14: shot_video_prompts.
# Each step is additive, so older files upgrade in place (see _migrate).
SCHEMA_VERSION = 14

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
CREATE TABLE IF NOT EXISTS design_options (
    option_id   TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS design_options_pkg ON design_options (package_id, revision_id);
CREATE TABLE IF NOT EXISTS exp_sessions (
    session_id  TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL,
    next_label  INTEGER NOT NULL DEFAULT 1  -- next image number (#N); saves don't touch it
);
CREATE TABLE IF NOT EXISTS exp_turns (
    turn_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    status      TEXT NOT NULL,              -- pending | running | done | failed
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exp_turns_session ON exp_turns (session_id, idx);
CREATE TABLE IF NOT EXISTS shot_takes (
    take_id     TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shot_id     TEXT NOT NULL,
    frame       TEXT NOT NULL,              -- the sub-shot number (v8: start | end)
    status      TEXT NOT NULL,              -- pending | writing | drawing | checking | done | failed
    created_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shot_takes_pkg ON shot_takes (package_id, revision_id, shot_id);
CREATE TABLE IF NOT EXISTS shot_prompts (
    version_id  TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shot_id     TEXT NOT NULL,
    sub_shot    INTEGER NOT NULL,
    v           INTEGER NOT NULL,           -- 1, 2, ... per shot and sub-shot
    data        TEXT NOT NULL,
    UNIQUE (package_id, revision_id, shot_id, sub_shot, v)
);
CREATE TABLE IF NOT EXISTS scene_readings (
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    scene_id    TEXT NOT NULL,
    status      TEXT NOT NULL,              -- writing | done | failed
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, revision_id, scene_id)
);
CREATE TABLE IF NOT EXISTS library_assets (
    asset_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- character | look | set | set_state | item
    parent_id   TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS library_assets_kind ON library_assets (kind, parent_id);
CREATE TABLE IF NOT EXISTS story_casting (
    package_id  TEXT NOT NULL,
    story_id    TEXT NOT NULL,              -- the story's character, look, location... id
    asset_id    TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, story_id)
);
CREATE INDEX IF NOT EXISTS story_casting_asset ON story_casting (asset_id);
CREATE TABLE IF NOT EXISTS asset_images (
    image_id    TEXT PRIMARY KEY,           -- a generated_images row, kept where it is
    asset_id    TEXT NOT NULL,
    package_id  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS asset_images_asset ON asset_images (asset_id);
CREATE TABLE IF NOT EXISTS story_settings (
    package_id  TEXT NOT NULL,     -- a story whose setting was changed after it was made
    name        TEXT NOT NULL,     -- rating (G, PG, PG-13, R) or pace (pace.py)
    value       TEXT NOT NULL,
    PRIMARY KEY (package_id, name)
);
CREATE TABLE IF NOT EXISTS image_ids (
    gid         INTEGER PRIMARY KEY AUTOINCREMENT,  -- the image's global number (@1234)
    ref         TEXT NOT NULL UNIQUE                -- what it is (image_ids.py)
);
CREATE TABLE IF NOT EXISTS shot_plan_versions (
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shot_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,           -- a replaced cut plan (shots/history.py)
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, revision_id, shot_id, version)
);
CREATE TABLE IF NOT EXISTS shot_video_prompts (
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shot_id     TEXT NOT NULL,
    status      TEXT NOT NULL,              -- writing | done | failed
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, revision_id, shot_id)
);
CREATE TABLE IF NOT EXISTS shot_plans (
    package_id  TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    shot_id     TEXT NOT NULL,
    status      TEXT NOT NULL,              -- writing | done | failed
    data        TEXT NOT NULL,
    PRIMARY KEY (package_id, revision_id, shot_id)
);
"""


# Jobs saved before the `archived` field existed have no key: not archived.
_NOT_ARCHIVED = "coalesce(json_extract(data, '$.archived'), 0) = 0"


# Tables of a story's work kept per revision; a published revise run moves them forward.
_REVISION_TABLES = (
    "generated_images", "design_options", "shot_takes", "shot_prompts", "shot_plans",
    "shot_plan_versions", "scene_readings", "shot_video_prompts",
)  # fmt: skip


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
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(exp_sessions)")}
        if "next_label" not in cols:
            self._conn.execute(
                "ALTER TABLE exp_sessions ADD COLUMN next_label INTEGER NOT NULL DEFAULT 1"
            )
        # Ratings changed before story_settings were kept in story_ratings (left in place).
        if self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'story_ratings'"
        ).fetchone():
            self._conn.execute(
                "INSERT OR IGNORE INTO story_settings (package_id, name, value) "
                "SELECT package_id, 'rating', rating FROM story_ratings"
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

    def replace_package(self, pkg: ProductionPackage) -> None:
        """Overwrite a stored revision: hand edits only (revisions are otherwise immutable)."""
        cur = self._exec(
            "UPDATE packages SET data = ? WHERE package_id = ? AND revision_id = ?",
            (pkg.model_dump_json(), pkg.package_id, pkg.revision_id),
        )
        if cur.rowcount == 0:
            raise KeyError(pkg.revision_id)

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

    def list_revisions(self, package_id: str | None = None) -> list[Revision]:
        """Revisions, oldest first (all packages when package_id is None)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM revisions WHERE ? IS NULL OR package_id = ? ORDER BY rowid",
                (package_id, package_id),
            ).fetchall()
        return [Revision.model_validate_json(r[0]) for r in rows]

    def carry_forward(self, package_id: str, from_revision: str, to_revision: str) -> int:
        """Move a story's design pictures, design options, keyframe takes, prompt versions,
        cut plans, scene readings and library picture links from one revision to the next
        (a published revise run). Ids stay the same, so picks and references keep working;
        what the revision changed is then judged out of date by its fingerprints. Returns
        the number of rows moved."""
        moved = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in _REVISION_TABLES:
                    # A kept plan version holds its plan, whose revision moves too.
                    nested = ", '$.plan.revision_id', ?" if table == "shot_plan_versions" else ""
                    cur = self._conn.execute(
                        f"UPDATE {table} SET revision_id = ?, "  # table names are fixed
                        f"data = json_set(data, '$.revision_id', ?{nested}) "
                        "WHERE package_id = ? AND revision_id = ?",
                        (to_revision, to_revision, *([to_revision] if nested else []),
                         package_id, from_revision),
                    )  # fmt: skip
                    moved += cur.rowcount
                # Library picture links keep their revision in the data only.
                cur = self._conn.execute(
                    "UPDATE asset_images SET data = json_set(data, '$.revision_id', ?) "
                    "WHERE package_id = ? AND json_extract(data, '$.revision_id') = ?",
                    (to_revision, package_id, from_revision),
                )
                moved += cur.rowcount
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return moved

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

    def set_selected(self, image_id: str) -> bool:
        """Pick this image for its deliverable and unpick the others, atomically."""
        return self._select(image_id, None)

    def move_selection(self, from_id: str, to_id: str) -> bool:
        """Move the pick from `from_id` to `to_id` (same deliverable), atomically, only if
        `from_id` is still the pick."""
        return self._select(to_id, from_id)

    def _select(self, image_id: str, only_if: str | None) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM generated_images WHERE image_id = ?", (image_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                target = GeneratedImage.model_validate_json(row[0])
                rows = self._conn.execute(
                    "SELECT data FROM generated_images WHERE package_id = ? AND revision_id = ?",
                    (target.package_id, target.revision_id),
                ).fetchall()
                same = [
                    i
                    for i in (GeneratedImage.model_validate_json(r[0]) for r in rows)
                    if (i.requirement_id, i.deliverable_id)
                    == (target.requirement_id, target.deliverable_id)
                ]
                if only_if is not None and not any(
                    i.image_id == only_if and i.selected for i in same
                ):
                    self._conn.execute("COMMIT")
                    return False
                for i in same:
                    want = i.image_id == image_id
                    if i.selected != want:
                        i.selected = want
                        self._conn.execute(
                            "UPDATE generated_images SET data = ? WHERE image_id = ?",
                            (i.model_dump_json(), i.image_id),
                        )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # Design options (takes) -------------------------------------------------

    def save_option(self, option: DesignOption) -> None:
        self._exec(
            "INSERT INTO design_options (option_id, package_id, revision_id, created_at, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (option_id) DO UPDATE SET data = excluded.data",
            (
                option.option_id,
                option.package_id,
                option.revision_id,
                option.created_at.isoformat(),
                option.model_dump_json(),
            ),
        )

    def delete_images(self, image_ids: list[str]) -> None:
        for i in image_ids:
            self._exec("DELETE FROM generated_images WHERE image_id = ?", (i,))

    def delete_option(self, option_id: str) -> None:
        self._exec("DELETE FROM design_options WHERE option_id = ?", (option_id,))

    def get_option(self, option_id: str) -> DesignOption | None:
        raw = self._one("SELECT data FROM design_options WHERE option_id = ?", (option_id,))
        return DesignOption.model_validate_json(raw) if raw else None

    def list_options(self, package_id: str, revision_id: str | None = None) -> list[DesignOption]:
        sql = "SELECT data FROM design_options WHERE package_id = ?"
        params: tuple[Any, ...] = (package_id,)
        if revision_id is not None:
            sql += " AND revision_id = ?"
            params += (revision_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, option_id", params).fetchall()
        return [DesignOption.model_validate_json(r[0]) for r in rows]

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

    # Experiment chat sessions ------------------------------------------------

    def save_exp_session(self, session: ExpSession) -> None:
        self._exec(
            "INSERT INTO exp_sessions (session_id, updated_at, data) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET updated_at = excluded.updated_at, "
            "data = excluded.data",
            (session.session_id, session.updated_at.isoformat(), session.model_dump_json()),
        )

    def get_exp_session(self, session_id: str) -> ExpSession | None:
        raw = self._one("SELECT data FROM exp_sessions WHERE session_id = ?", (session_id,))
        return ExpSession.model_validate_json(raw) if raw else None

    def list_exp_sessions(self) -> list[ExpSession]:
        """Most recently active first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM exp_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [ExpSession.model_validate_json(r[0]) for r in rows]

    def delete_exp_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM exp_turns WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM exp_sessions WHERE session_id = ?", (session_id,))
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def save_exp_turn(self, turn: ExpTurn) -> None:
        self._exec(
            "INSERT INTO exp_turns (turn_id, session_id, idx, status, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (turn_id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (turn.turn_id, turn.session_id, turn.index, turn.status, turn.model_dump_json()),
        )

    def get_exp_turn(self, turn_id: str) -> ExpTurn | None:
        raw = self._one("SELECT data FROM exp_turns WHERE turn_id = ?", (turn_id,))
        return ExpTurn.model_validate_json(raw) if raw else None

    def list_exp_turns(self, session_id: str) -> list[ExpTurn]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM exp_turns WHERE session_id = ? ORDER BY idx", (session_id,)
            ).fetchall()
        return [ExpTurn.model_validate_json(r[0]) for r in rows]

    def fail_unfinished_exp_turns(self, error: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM exp_turns WHERE status IN ('pending', 'running')"
            ).fetchall()
        for (raw,) in rows:
            t = ExpTurn.model_validate_json(raw)
            t.status, t.error, t.finished_at = "failed", error, utcnow()
            self.save_exp_turn(t)
        return len(rows)

    def allocate_exp_labels(self, session_id: str, n: int) -> list[int]:
        """The next n image numbers of a chat (1, 2, ...), never handed out twice. One
        atomic UPDATE, so the API and the runner can't hand out the same number."""
        with self._lock:
            row = self._conn.execute(
                "UPDATE exp_sessions SET next_label = next_label + ? WHERE session_id = ? "
                "RETURNING next_label",
                (n, session_id),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        end = row[0]
        return list(range(end - n, end))

    # Global image numbers (image_ids.py) ---------------------------------------

    def image_ids(self, refs: list[str]) -> dict[str, int]:
        """Each picture's global number, handing out the next ones to those without."""
        if not refs:
            return {}
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO image_ids (ref) VALUES (?)", [(r,) for r in refs]
            )
            out: dict[str, int] = {}
            for i in range(0, len(refs), 500):
                chunk = refs[i : i + 500]
                marks = ",".join("?" * len(chunk))
                out.update(
                    self._conn.execute(
                        f"SELECT ref, gid FROM image_ids WHERE ref IN ({marks})", chunk
                    ).fetchall()
                )
        return out

    def image_ref(self, gid: int) -> str | None:
        return self._one("SELECT ref FROM image_ids WHERE gid = ?", (gid,))

    # A story's settings changed after it was made (story_settings.py): its rating, its pace.
    # Kept apart from the jobs, which a running pipeline saves from its own copy.

    def get_story_setting(self, package_id: str, name: str) -> str | None:
        return self._one(
            "SELECT value FROM story_settings WHERE package_id = ? AND name = ?", (package_id, name)
        )

    def set_story_setting(self, package_id: str, name: str, value: str) -> None:
        self._exec(
            "INSERT INTO story_settings (package_id, name, value) VALUES (?, ?, ?) "
            "ON CONFLICT (package_id, name) DO UPDATE SET value = excluded.value",
            (package_id, name, value),
        )

    # Shot keyframes ------------------------------------------------------------

    def save_shot_take(self, take: ShotTake) -> None:
        """Insert or update a take. An update never changes `approved`: only
        approve_shot_draw does, so a runner saving its copy can't undo an approval."""
        self._exec(
            "INSERT INTO shot_takes (take_id, package_id, revision_id, shot_id, frame, status, "
            "created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (take_id) DO UPDATE "
            "SET status = excluded.status, frame = excluded.frame, data = json_set(excluded.data, '$.approved', "
            "json_extract(shot_takes.data, '$.approved'))",
            (take.take_id, take.package_id, take.revision_id, take.shot_id, str(take.sub_shot),
             take.status, take.created_at.isoformat(), take.model_dump_json()),
        )  # fmt: skip

    def get_shot_take(self, take_id: str) -> ShotTake | None:
        raw = self._one("SELECT data FROM shot_takes WHERE take_id = ?", (take_id,))
        return ShotTake.model_validate_json(raw) if raw else None

    def list_shot_takes(self, package_id: str, revision_id: str) -> list[ShotTake]:
        """Oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_takes WHERE package_id = ? AND revision_id = ? "
                "ORDER BY created_at, rowid",
                (package_id, revision_id),
            ).fetchall()
        return [ShotTake.model_validate_json(r[0]) for r in rows]

    def delete_shot_take(self, take_id: str) -> None:
        self._exec("DELETE FROM shot_takes WHERE take_id = ?", (take_id,))

    def approve_shot_draw(self, take_id: str, n: int | None) -> ShotTake:
        """Approve draw `n` of a take (None: withdraw the approval), in one transaction:
        see shot_frames.approval_changes."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                raw = self._conn.execute(
                    "SELECT data FROM shot_takes WHERE take_id = ?", (take_id,)
                ).fetchone()
                if raw is None:
                    raise KeyError(take_id)
                take = ShotTake.model_validate_json(raw[0])
                rows = self._conn.execute(
                    "SELECT data FROM shot_takes WHERE package_id = ? AND revision_id = ? "
                    "AND shot_id = ? AND take_id != ?",
                    (take.package_id, take.revision_id, take.shot_id, take_id),
                ).fetchall()
                others = [ShotTake.model_validate_json(r[0]) for r in rows]
                for t in approval_changes(take, n, others):
                    self._conn.execute(
                        "UPDATE shot_takes SET data = ? WHERE take_id = ?",
                        (t.model_dump_json(), t.take_id),
                    )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return take

    def save_shot_plan(self, plan: ShotPlan) -> None:
        self._exec(
            "INSERT INTO shot_plans (package_id, revision_id, shot_id, status, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (package_id, revision_id, shot_id) DO UPDATE "
            "SET status = excluded.status, data = excluded.data",
            (plan.package_id, plan.revision_id, plan.shot_id, plan.status,
             plan.model_dump_json()),
        )  # fmt: skip

    def get_shot_plan(self, package_id: str, revision_id: str, shot_id: str) -> ShotPlan | None:
        raw = self._one(
            "SELECT data FROM shot_plans WHERE package_id = ? AND revision_id = ? AND shot_id = ?",
            (package_id, revision_id, shot_id),
        )
        return ShotPlan.model_validate_json(raw) if raw else None

    def list_shot_plans(self, package_id: str, revision_id: str) -> list[ShotPlan]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_plans WHERE package_id = ? AND revision_id = ?",
                (package_id, revision_id),
            ).fetchall()
        return [ShotPlan.model_validate_json(r[0]) for r in rows]

    def add_plan_version(self, v: ShotPlanVersion) -> ShotPlanVersion:
        """Store a replaced plan, numbered next for its shot (in one transaction, so the
        planner and an edit or a restore never get the same number)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                (last,) = self._conn.execute(
                    "SELECT coalesce(max(version), 0) FROM shot_plan_versions "
                    "WHERE package_id = ? AND revision_id = ? AND shot_id = ?",
                    (v.package_id, v.revision_id, v.shot_id),
                ).fetchone()
                v = v.model_copy(update={"version": last + 1})
                self._conn.execute(
                    "INSERT INTO shot_plan_versions (package_id, revision_id, shot_id, version, "
                    "data) VALUES (?, ?, ?, ?, ?)",
                    (v.package_id, v.revision_id, v.shot_id, v.version, v.model_dump_json()),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return v

    def delete_plan_version(
        self, package_id: str, revision_id: str, shot_id: str, version: int
    ) -> None:
        self._exec(
            "DELETE FROM shot_plan_versions WHERE package_id = ? AND revision_id = ? "
            "AND shot_id = ? AND version = ?",
            (package_id, revision_id, shot_id, version),
        )

    def count_plan_versions(self, package_id: str, revision_id: str) -> dict[str, int]:
        """How many earlier plans each shot of a revision has (for the board)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT shot_id, count(*) FROM shot_plan_versions "
                "WHERE package_id = ? AND revision_id = ? GROUP BY shot_id",
                (package_id, revision_id),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def list_plan_versions(
        self, package_id: str, revision_id: str, shot_id: str | None = None
    ) -> list[ShotPlanVersion]:
        """A revision's replaced cut plans (of one shot, if given), oldest first."""
        sql = "SELECT data FROM shot_plan_versions WHERE package_id = ? AND revision_id = ?"
        params: tuple[Any, ...] = (package_id, revision_id)
        if shot_id is not None:
            sql, params = sql + " AND shot_id = ?", (*params, shot_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY shot_id, version", params).fetchall()
        return [ShotPlanVersion.model_validate_json(r[0]) for r in rows]

    def fail_unfinished_shot_plans(self, error: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_plans WHERE status = 'writing'"
            ).fetchall()
        for (raw,) in rows:
            p = ShotPlan.model_validate_json(raw)
            p.status, p.error = "failed", error
            self.save_shot_plan(p)
        return len(rows)

    def add_prompt_version(self, version: PromptVersion) -> PromptVersion:
        """Store a new prompt version, numbered next for its shot and sub-shot (in one
        transaction, so the writer and an editor never get the same number)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                (last,) = self._conn.execute(
                    "SELECT coalesce(max(v), 0) FROM shot_prompts WHERE package_id = ? "
                    "AND revision_id = ? AND shot_id = ? AND sub_shot = ?",
                    (version.package_id, version.revision_id, version.shot_id,
                     version.sub_shot),
                ).fetchone()  # fmt: skip
                version = version.model_copy(update={"v": last + 1})
                self._conn.execute(
                    "INSERT INTO shot_prompts (version_id, package_id, revision_id, shot_id, "
                    "sub_shot, v, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (version.version_id, version.package_id, version.revision_id,
                     version.shot_id, version.sub_shot, version.v, version.model_dump_json()),
                )  # fmt: skip
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return version

    def list_prompt_versions(self, package_id: str, revision_id: str) -> list[PromptVersion]:
        """Every keyframe prompt version of a package revision, oldest first per sub-shot."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_prompts WHERE package_id = ? AND revision_id = ? "
                "ORDER BY shot_id, sub_shot, v",
                (package_id, revision_id),
            ).fetchall()
        return [PromptVersion.model_validate_json(r[0]) for r in rows]

    def save_scene_reading(self, reading: SceneReading) -> None:
        self._exec(
            "INSERT INTO scene_readings (package_id, revision_id, scene_id, status, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (package_id, revision_id, scene_id) DO UPDATE "
            "SET status = excluded.status, data = excluded.data",
            (reading.package_id, reading.revision_id, reading.scene_id, reading.status,
             reading.model_dump_json()),
        )  # fmt: skip

    def get_scene_reading(
        self, package_id: str, revision_id: str, scene_id: str
    ) -> SceneReading | None:
        raw = self._one(
            "SELECT data FROM scene_readings WHERE package_id = ? AND revision_id = ? "
            "AND scene_id = ?",
            (package_id, revision_id, scene_id),
        )
        return SceneReading.model_validate_json(raw) if raw else None

    def list_scene_readings(self, package_id: str, revision_id: str) -> list[SceneReading]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM scene_readings WHERE package_id = ? AND revision_id = ?",
                (package_id, revision_id),
            ).fetchall()
        return [SceneReading.model_validate_json(r[0]) for r in rows]

    # The asset library ------------------------------------------------------

    def list_package_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT package_id FROM packages GROUP BY package_id ORDER BY min(seq)"
            ).fetchall()
        return [r[0] for r in rows]

    def save_library_asset(self, asset: LibraryAsset) -> None:
        self._exec(
            "INSERT INTO library_assets (asset_id, kind, parent_id, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (asset_id) DO UPDATE SET kind = excluded.kind, "
            "parent_id = excluded.parent_id, data = excluded.data",
            (asset.asset_id, asset.kind, asset.parent_id, asset.model_dump_json()),
        )

    def get_library_asset(self, asset_id: str) -> LibraryAsset | None:
        raw = self._one("SELECT data FROM library_assets WHERE asset_id = ?", (asset_id,))
        return LibraryAsset.model_validate_json(raw) if raw else None

    def list_library_assets(
        self, kind: str | None = None, parent_id: str | None = None
    ) -> list[LibraryAsset]:
        sql, args = "SELECT data FROM library_assets WHERE 1 = 1", []
        if kind is not None:
            sql, args = sql + " AND kind = ?", [*args, kind]
        if parent_id is not None:
            sql, args = sql + " AND parent_id = ?", [*args, parent_id]
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [LibraryAsset.model_validate_json(r[0]) for r in rows]

    def save_casting(self, casting: Casting) -> None:
        self._exec(
            "INSERT INTO story_casting (package_id, story_id, asset_id, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (package_id, story_id) DO UPDATE SET asset_id = excluded.asset_id, "
            "data = excluded.data",
            (casting.package_id, casting.story_id, casting.asset_id, casting.model_dump_json()),
        )

    def list_casting(
        self, package_id: str | None = None, asset_id: str | None = None
    ) -> list[Casting]:
        sql, args = "SELECT data FROM story_casting WHERE 1 = 1", []
        if package_id is not None:
            sql, args = sql + " AND package_id = ?", [*args, package_id]
        if asset_id is not None:
            sql, args = sql + " AND asset_id = ?", [*args, asset_id]
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [Casting.model_validate_json(r[0]) for r in rows]

    def link_asset_image(self, link: AssetImage) -> None:
        self._exec(
            "INSERT INTO asset_images (image_id, asset_id, package_id, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (image_id) DO UPDATE SET asset_id = excluded.asset_id, "
            "package_id = excluded.package_id, data = excluded.data",
            (link.image_id, link.asset_id, link.package_id, link.model_dump_json()),
        )

    def list_asset_images(
        self, asset_id: str | None = None, package_id: str | None = None
    ) -> list[AssetImage]:
        sql, args = "SELECT data FROM asset_images WHERE 1 = 1", []
        if asset_id is not None:
            sql, args = sql + " AND asset_id = ?", [*args, asset_id]
        if package_id is not None:
            sql, args = sql + " AND package_id = ?", [*args, package_id]
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [AssetImage.model_validate_json(r[0]) for r in rows]

    def fail_unfinished_scene_readings(self, error: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM scene_readings WHERE status = 'writing'"
            ).fetchall()
        for (raw,) in rows:
            r = SceneReading.model_validate_json(raw)
            r.status, r.error = "failed", error
            self.save_scene_reading(r)
        return len(rows)

    def save_video_prompt(self, prompt: ShotVideoPrompt) -> None:
        self._exec(
            "INSERT INTO shot_video_prompts (package_id, revision_id, shot_id, status, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (package_id, revision_id, shot_id) DO UPDATE "
            "SET status = excluded.status, data = excluded.data",
            (prompt.package_id, prompt.revision_id, prompt.shot_id, prompt.status,
             prompt.model_dump_json()),
        )  # fmt: skip

    def get_video_prompt(
        self, package_id: str, revision_id: str, shot_id: str
    ) -> ShotVideoPrompt | None:
        raw = self._one(
            "SELECT data FROM shot_video_prompts WHERE package_id = ? AND revision_id = ? "
            "AND shot_id = ?",
            (package_id, revision_id, shot_id),
        )
        return ShotVideoPrompt.model_validate_json(raw) if raw else None

    def list_video_prompts(self, package_id: str, revision_id: str) -> list[ShotVideoPrompt]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_video_prompts WHERE package_id = ? AND revision_id = ?",
                (package_id, revision_id),
            ).fetchall()
        return [ShotVideoPrompt.model_validate_json(r[0]) for r in rows]

    def fail_unfinished_video_prompts(self, error: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_video_prompts WHERE status = 'writing'"
            ).fetchall()
        for (raw,) in rows:
            p = ShotVideoPrompt.model_validate_json(raw)
            p.status, p.error = "failed", error
            self.save_video_prompt(p)
        return len(rows)

    def fail_unfinished_shot_takes(self, error: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM shot_takes WHERE status NOT IN ('done', 'failed', 'paused')"
            ).fetchall()
        for (raw,) in rows:
            t = ShotTake.model_validate_json(raw)
            t.status, t.error, t.finished_at = "failed", error, utcnow()
            self.save_shot_take(t)
        return len(rows)

    def asset_in_use(self, asset_id: str) -> bool:
        """Whether a job, a reference image or a chat message uses this upload. Asset ids
        are random and unique, so finding the id in a stored document means it's used."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM jobs WHERE instr(data, ?) "
                "UNION ALL SELECT 1 FROM generated_images WHERE instr(data, ?) "
                "UNION ALL SELECT 1 FROM exp_turns WHERE instr(data, ?) LIMIT 1",
                (asset_id, asset_id, asset_id),
            ).fetchone()
        return row is not None

    def delete_source_asset(self, asset_id: str) -> None:
        self._exec("DELETE FROM source_assets WHERE asset_id = ?", (asset_id,))
