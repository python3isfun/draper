"""Durable job queue with leases, retries and per-job pipeline checkpoints.

A worker claims a job for `lease_s` seconds and renews the lease while it runs. If the
worker dies, the lease expires and another worker reclaims the job; the pipeline resumes
from its last checkpoint instead of re-running finished (expensive) model steps.

Each claim counts as one attempt. A graceful shutdown releases the lease without counting
the attempt. When attempts run out the entry becomes `dead` and the job is failed.

Queue states: queued -> leased -> done | dead (leased -> queued on retry or release),
plus paused: a queued job pauses at once; a leased one gets `pause_requested` and the
pipeline pauses at its next safe point (before a model call). Pausing never spends an
attempt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .store.sqlite import SqliteRepository


@dataclass(frozen=True)
class Claim:
    job_id: str
    attempt: int  # 1-based
    max_attempts: int
    # True when the previous holder's lease expired (worker crashed or was killed).
    recovered: bool = False
    # True when this claim found no attempts left; the worker must fail the job.
    exhausted: bool = False


# Values of the pause flag: pause at the next safe point (after the current model call), or
# stop now (the worker interrupts the running step).
PAUSE_AT_SAFE_POINT = 1
STOP_NOW = 2


class JobQueue(Protocol):
    max_attempts: int

    def enqueue(self, job_id: str) -> None:
        """Idempotent: re-enqueueing a known job is a no-op."""

    def claim(self, worker_id: str, lease_s: float) -> Claim | None: ...
    def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        """Extend the lease. False means this worker no longer owns the job."""

    def complete(self, job_id: str, worker_id: str) -> None: ...
    def retry(self, job_id: str, worker_id: str, error: str, delay_s: float) -> bool:
        """Requeue after a failure. False when attempts are exhausted (entry is dead)."""

    def release(self, job_id: str, worker_id: str) -> None:
        """Give the job back without counting the attempt (graceful shutdown)."""

    def restart(self, job_id: str) -> None:
        """Queue a finished (done/dead) job again with a fresh attempt budget, keeping its
        checkpoint. Raises ValueError if the job is still queued or running."""

    def request_pause(self, job_id: str, now: bool = False) -> str:
        """'paused' if the job was waiting (now paused), 'requested' if it's running and will
        pause at its next safe point (now=True: the worker interrupts the current model call
        instead). Raises ValueError if it can't be paused."""

    def pause_requested(self, job_id: str) -> bool: ...
    def stop_requested(self, job_id: str) -> bool:
        """A pause that interrupts the running step (request_pause(now=True))."""

    def pause(self, job_id: str, worker_id: str) -> None:
        """The worker reached a safe point: park the job (attempt not counted)."""

    def resume(self, job_id: str) -> None:
        """Queue a paused job again. Raises ValueError if it isn't paused."""

    def state(self, job_id: str) -> str | None: ...
    def save_checkpoint(self, job_id: str, data: str) -> None: ...
    def load_checkpoint(self, job_id: str) -> str | None: ...


# ---------------------------------------------------------------------------
# In-memory (tests, DRAMA_STORE=memory). Same semantics, not durable.


@dataclass
class _Entry:
    state: str = "queued"
    attempts: int = 0
    available_at: float = 0.0
    enqueued_at: float = 0.0
    lease_owner: str | None = None
    lease_expires_at: float = 0.0
    last_error: str | None = None
    checkpoint: str | None = None
    pause_requested: int = 0  # 0, PAUSE_AT_SAFE_POINT or STOP_NOW


class InMemoryJobQueue:
    def __init__(self, max_attempts: int = 3) -> None:
        self.max_attempts = max_attempts
        self.entries: dict[str, _Entry] = {}

    def enqueue(self, job_id: str) -> None:
        if job_id not in self.entries:
            now = time.time()
            self.entries[job_id] = _Entry(available_at=now, enqueued_at=now)

    def claim(self, worker_id: str, lease_s: float) -> Claim | None:
        now = time.time()
        ready = [
            (e.enqueued_at, jid, e)
            for jid, e in self.entries.items()
            if (e.state == "queued" and e.available_at <= now)
            or (e.state == "leased" and e.lease_expires_at < now)
        ]
        if not ready:
            return None
        _, job_id, e = min(ready, key=lambda r: r[0])
        recovered = e.state == "leased"
        if e.attempts >= self.max_attempts:
            e.state, e.lease_owner = "dead", None
            return Claim(job_id, e.attempts, self.max_attempts, recovered, exhausted=True)
        e.attempts += 1
        e.state, e.lease_owner, e.lease_expires_at = "leased", worker_id, now + lease_s
        return Claim(job_id, e.attempts, self.max_attempts, recovered)

    def _owned(self, job_id: str, worker_id: str) -> _Entry | None:
        e = self.entries.get(job_id)
        return e if e and e.state == "leased" and e.lease_owner == worker_id else None

    def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        e = self._owned(job_id, worker_id)
        if e:
            e.lease_expires_at = time.time() + lease_s
        return e is not None

    def complete(self, job_id: str, worker_id: str) -> None:
        if e := self._owned(job_id, worker_id):
            e.state, e.lease_owner = "done", None

    def retry(self, job_id: str, worker_id: str, error: str, delay_s: float) -> bool:
        e = self._owned(job_id, worker_id)
        if e is None:
            return False
        e.last_error, e.lease_owner = error, None
        if e.attempts >= self.max_attempts:
            e.state = "dead"
            return False
        e.state, e.available_at = "queued", time.time() + delay_s
        return True

    def release(self, job_id: str, worker_id: str) -> None:
        if e := self._owned(job_id, worker_id):
            e.state, e.lease_owner, e.attempts = "queued", None, max(e.attempts - 1, 0)
            e.available_at = time.time()

    def restart(self, job_id: str) -> None:
        e = self.entries.get(job_id)
        if e is None:
            self.enqueue(job_id)
            return
        if e.state in ("queued", "leased"):
            raise ValueError(f"job {job_id} is still {e.state}")
        e.state, e.attempts, e.lease_owner, e.available_at = "queued", 0, None, time.time()

    def request_pause(self, job_id: str, now: bool = False) -> str:
        e = self.entries.get(job_id)
        if e is None or e.state not in ("queued", "leased"):
            raise ValueError(f"job {job_id} is not queued or running")
        if e.state == "queued":
            e.state = "paused"
            return "paused"
        e.pause_requested = max(int(e.pause_requested), STOP_NOW if now else PAUSE_AT_SAFE_POINT)
        return "requested"

    def pause_requested(self, job_id: str) -> bool:
        e = self.entries.get(job_id)
        return bool(e and e.pause_requested)

    def stop_requested(self, job_id: str) -> bool:
        e = self.entries.get(job_id)
        return bool(e and int(e.pause_requested) >= STOP_NOW)

    def pause(self, job_id: str, worker_id: str) -> None:
        if e := self._owned(job_id, worker_id):
            e.state, e.lease_owner, e.pause_requested = "paused", None, False
            e.attempts = max(e.attempts - 1, 0)

    def resume(self, job_id: str) -> None:
        e = self.entries.get(job_id)
        if e is None or e.state != "paused":
            raise ValueError(f"job {job_id} is not paused")
        e.state, e.available_at, e.pause_requested = "queued", time.time(), False

    def state(self, job_id: str) -> str | None:
        e = self.entries.get(job_id)
        return e.state if e else None

    def save_checkpoint(self, job_id: str, data: str) -> None:
        self.entries.setdefault(job_id, _Entry()).checkpoint = data

    def load_checkpoint(self, job_id: str) -> str | None:
        e = self.entries.get(job_id)
        return e.checkpoint if e else None


# ---------------------------------------------------------------------------
# SQLite (default). Shares the repository's connection, so it lives in the same file;
# separate worker processes open their own repository on that file.


class SqliteJobQueue:
    def __init__(self, repo: SqliteRepository, max_attempts: int = 3) -> None:
        self.max_attempts = max_attempts
        self._conn = repo._conn
        self._lock = repo._lock

    def enqueue(self, job_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO job_queue (job_id, state, available_at, enqueued_at) "
                "VALUES (?, 'queued', ?, ?)",
                (job_id, now, now),
            )

    def claim(self, worker_id: str, lease_s: float) -> Claim | None:
        now = time.time()
        with self._lock:
            # IMMEDIATE takes the write lock up front, so two processes can't both claim.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT job_id, state, attempts FROM job_queue "
                    "WHERE (state = 'queued' AND available_at <= ?) "
                    "   OR (state = 'leased' AND lease_expires_at < ?) "
                    "ORDER BY enqueued_at LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                job_id, state, attempts = row
                recovered = state == "leased"
                if attempts >= self.max_attempts:
                    self._conn.execute(
                        "UPDATE job_queue SET state = 'dead', lease_owner = NULL WHERE job_id = ?",
                        (job_id,),
                    )
                    self._conn.execute("COMMIT")
                    return Claim(job_id, attempts, self.max_attempts, recovered, exhausted=True)
                self._conn.execute(
                    "UPDATE job_queue SET state = 'leased', attempts = attempts + 1, "
                    "lease_owner = ?, lease_expires_at = ? WHERE job_id = ?",
                    (worker_id, now + lease_s, job_id),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return Claim(job_id, attempts + 1, self.max_attempts, recovered)

    def _update_owned(self, job_id: str, worker_id: str, sets: str, params: tuple) -> bool:
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE job_queue SET {sets} "  # `sets` is a constant from this class
                "WHERE job_id = ? AND state = 'leased' AND lease_owner = ?",
                (*params, job_id, worker_id),
            )
        return cur.rowcount == 1

    def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        return self._update_owned(
            job_id, worker_id, "lease_expires_at = ?", (time.time() + lease_s,)
        )

    def complete(self, job_id: str, worker_id: str) -> None:
        self._update_owned(job_id, worker_id, "state = 'done', lease_owner = NULL", ())

    def retry(self, job_id: str, worker_id: str, error: str, delay_s: float) -> bool:
        requeued = self._update_owned(
            job_id,
            worker_id,
            "state = CASE WHEN attempts >= ? THEN 'dead' ELSE 'queued' END, "
            "available_at = ?, last_error = ?, lease_owner = NULL",
            (self.max_attempts, time.time() + delay_s, error[:2000]),
        )
        return requeued and self.state(job_id) == "queued"

    def release(self, job_id: str, worker_id: str) -> None:
        self._update_owned(
            job_id,
            worker_id,
            "state = 'queued', lease_owner = NULL, available_at = ?, "
            "attempts = MAX(attempts - 1, 0)",
            (time.time(),),
        )

    def restart(self, job_id: str) -> None:
        current = self.state(job_id)
        if current is None:
            self.enqueue(job_id)
            return
        with self._lock:
            cur = self._conn.execute(
                "UPDATE job_queue SET state = 'queued', attempts = 0, lease_owner = NULL, "
                "available_at = ? WHERE job_id = ? AND state IN ('done', 'dead')",
                (time.time(), job_id),
            )
        if cur.rowcount != 1:
            raise ValueError(f"job {job_id} is still {current}")

    def request_pause(self, job_id: str, now: bool = False) -> str:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE job_queue SET state = 'paused' WHERE job_id = ? AND state = 'queued'",
                (job_id,),
            )
            if cur.rowcount == 1:
                return "paused"
            cur = self._conn.execute(
                "UPDATE job_queue SET pause_requested = MAX(pause_requested, ?) "
                "WHERE job_id = ? AND state = 'leased'",
                (STOP_NOW if now else PAUSE_AT_SAFE_POINT, job_id),
            )
        if cur.rowcount == 1:
            return "requested"
        raise ValueError(f"job {job_id} is not queued or running")

    def pause_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT pause_requested FROM job_queue WHERE job_id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def stop_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT pause_requested FROM job_queue WHERE job_id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0] >= STOP_NOW)

    def pause(self, job_id: str, worker_id: str) -> None:
        self._update_owned(
            job_id,
            worker_id,
            "state = 'paused', lease_owner = NULL, pause_requested = 0, "
            "attempts = MAX(attempts - 1, 0)",
            (),
        )

    def resume(self, job_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE job_queue SET state = 'queued', available_at = ?, pause_requested = 0 "
                "WHERE job_id = ? AND state = 'paused'",
                (time.time(), job_id),
            )
        if cur.rowcount != 1:
            raise ValueError(f"job {job_id} is not paused")

    def state(self, job_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM job_queue WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row[0] if row else None

    def save_checkpoint(self, job_id: str, data: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE job_queue SET checkpoint = ? WHERE job_id = ?", (data, job_id)
            )

    def load_checkpoint(self, job_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT checkpoint FROM job_queue WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row[0] if row else None
