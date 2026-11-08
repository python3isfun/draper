"""Job workers: claim jobs from the durable queue and run them through the pipeline.

Workers run inside the API process by default (DRAMA_WORKERS, default 1). To scale or
isolate them, set DRAMA_WORKERS=0 for the API and run workers separately against the same
SQLite file:

    python -m drama_agent.worker            # concurrency from DRAMA_WORKERS (default 1)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import uuid

from .jobqueue import Claim, JobQueue
from .models import Job
from .pipeline.orchestrator import Orchestrator, new_id
from .pipeline.steps import PipelineCheckpoint
from .store import Repository
from .tracing import Tracer

log = logging.getLogger(__name__)

TERMINAL = ("completed", "needs_revision", "failed", "cancelled")
PAUSABLE = ("queued", "running")


class JobWorker:
    def __init__(
        self,
        repo: Repository,
        queue: JobQueue,
        orchestrator: Orchestrator,
        *,
        worker_id: str | None = None,
        lease_s: float = 30.0,
        poll_s: float = 1.0,
        retry_base_s: float = 5.0,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.orchestrator = orchestrator
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.retry_base_s = retry_base_s

    async def run(self, stop: asyncio.Event, wakeup: asyncio.Event | None = None) -> None:
        """Process jobs until `stop` is set. `wakeup` lets enqueuers skip the poll delay."""
        log.info("worker %s started", self.worker_id)
        try:
            while not stop.is_set():
                claim = self.queue.claim(self.worker_id, self.lease_s)
                if claim is None:
                    await _wait_any(stop, wakeup, self.poll_s)
                    continue
                await self.process(claim)
        finally:
            # Also reached on cancellation (shutdown), after any in-flight job is released.
            log.info("worker %s stopped", self.worker_id)

    async def run_once(self) -> bool:
        """Claim and process one job if any is ready. Returns whether one was processed."""
        claim = self.queue.claim(self.worker_id, self.lease_s)
        if claim is None:
            return False
        await self.process(claim)
        return True

    async def process(self, claim: Claim) -> None:
        job = self.repo.get_job(claim.job_id)
        if job is None:
            log.error("queue entry %s has no job; dropping", claim.job_id)
            self.queue.complete(claim.job_id, self.worker_id)
            return
        if claim.exhausted:
            # Last holder died on its final attempt.
            job.status = "failed"
            job.failure_reason = (
                f"gave up after {claim.attempt} attempts; the last worker stopped responding"
            )
            self.repo.save_job(job)
            Tracer(self.repo, claim.job_id).event("job_failed", job.failure_reason)
            return
        if job.status in TERMINAL:
            self.queue.complete(claim.job_id, self.worker_id)
            return

        trace = Tracer(self.repo, claim.job_id)
        if claim.recovered:
            log.warning("job %s: previous worker's lease expired; resuming", claim.job_id)
            trace.event("recovered", "The previous worker stopped responding; picked the job up.")
        job.attempts = claim.attempt
        self.repo.save_job(job)

        run = asyncio.create_task(self.orchestrator.run_job(claim.job_id))
        lost_lease = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(claim.job_id, run, lost_lease))
        try:
            job = await run
        except asyncio.CancelledError:
            if lost_lease.is_set():
                log.warning("job %s: lease lost to another worker; stopped", claim.job_id)
                trace.event("lease_lost", "Another worker took over this job; stopped here.")
                return
            # Shutdown: hand the job back without spending an attempt. The checkpoint
            # lets the next worker resume after the last passed step.
            self.queue.release(claim.job_id, self.worker_id)
            self._mark_queued(claim.job_id, "worker shut down; job requeued")
            trace.event("requeued", "The worker shut down; the job is back in the queue.")
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        if job.status == "paused":
            # Parked until resumed; pausing doesn't spend an attempt.
            self.queue.pause(claim.job_id, self.worker_id)
            return
        if job.status != "failed":
            self.queue.complete(claim.job_id, self.worker_id)
            return

        delay = self.retry_base_s * 2 ** (claim.attempt - 1)
        reason = job.failure_reason or "unknown error"
        if self.queue.retry(claim.job_id, self.worker_id, reason, delay):
            message = (
                f"attempt {claim.attempt}/{claim.max_attempts} failed: {reason}; "
                f"retrying in {delay:.0f}s"
            )
            self._mark_queued(claim.job_id, message)
            trace.event("retry_scheduled", message[0].upper() + message[1:] + ".")
        else:
            job.failure_reason = f"failed after {claim.attempt} attempts: {reason}"
            self.repo.save_job(job)
            trace.event("gave_up", f"Gave up after {claim.attempt} attempts.")

    async def _heartbeat(self, job_id: str, run: asyncio.Task, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            if not self.queue.heartbeat(job_id, self.worker_id, self.lease_s):
                lost.set()
                run.cancel()
                return

    def _mark_queued(self, job_id: str, reason: str) -> None:
        job = self.repo.get_job(job_id)
        if job is None:
            return
        job.status = "queued"
        job.failure_reason = reason
        for stage in job.stages.values():
            if stage.status in ("running", "failed"):
                stage.status = "pending"
        self.repo.save_job(job)


async def _wait_any(stop: asyncio.Event, wakeup: asyncio.Event | None, timeout: float) -> None:
    waits = [asyncio.create_task(stop.wait())]
    if wakeup is not None:
        waits.append(asyncio.create_task(wakeup.wait()))
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()
    if wakeup is not None:
        wakeup.clear()


RETRYABLE = ("needs_revision", "failed")


def retry_job(repo: Repository, queue: JobQueue, job_id: str) -> Job:
    """Run a finished job again, resuming after the steps its checkpoint says passed.

    The retry saves under a new revision ID: the failed run may already have stored a
    draft under the checkpoint's ID, and revisions are immutable. Raises KeyError for an
    unknown job and ValueError if the job can't be retried.
    """
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.status not in RETRYABLE:
        raise ValueError(f"only {' or '.join(RETRYABLE)} jobs can be retried, not {job.status}")

    passed: list[str] = []
    if raw := queue.load_checkpoint(job_id):
        cp = PipelineCheckpoint.model_validate_json(raw)
        cp.revision_id = new_id("rev")
        queue.save_checkpoint(job_id, cp.model_dump_json())
        passed = cp.completed_stages

    queue.restart(job_id)  # raises ValueError if somehow still queued/leased
    for name, stage in job.stages.items():
        if name not in passed:  # the checkpoint, not the job record, is the source of truth
            stage.status, stage.attempts, stage.issues, stage.error = "pending", 0, [], None
    job.status = "queued"
    job.attempts = 0
    job.package_id = job.revision_id = None
    job.failure_reason = (
        f"retry requested; keeping {', '.join(passed)}" if passed else "retry requested"
    )
    repo.save_job(job)
    Tracer(repo, job_id).event(
        "retry_requested", job.failure_reason[0].upper() + job.failure_reason[1:] + "."
    )
    return job


def pause_job(repo: Repository, queue: JobQueue, job_id: str) -> Job:
    """Pause now if the job is waiting, otherwise at the running pipeline's next safe point
    (after the current model call). Raises KeyError / ValueError."""
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.status not in PAUSABLE:
        raise ValueError(f"only queued or running jobs can be paused, not {job.status}")
    outcome = queue.request_pause(job_id)
    trace = Tracer(repo, job_id)
    if outcome == "paused":
        job.status = "paused"
        repo.save_job(job)
        trace.event("paused", "Paused before it started.")
    else:
        trace.event("pause_requested", "Pause requested; stopping after the current model call.")
    job.pause_requested = outcome == "requested"
    return job


def resume_job(repo: Repository, queue: JobQueue, job_id: str) -> Job:
    """Continue a paused job from its checkpoint. Raises KeyError / ValueError."""
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.status != "paused":
        raise ValueError(f"only paused jobs can be resumed, not {job.status}")
    queue.resume(job_id)
    job.status = "queued"
    job.failure_reason = None
    repo.save_job(job)
    Tracer(repo, job_id).event("resumed", "Resumed by the user; waiting for a worker.")
    return job


def recover(repo: Repository, queue: JobQueue) -> int:
    """Enqueue unfinished jobs that have no queue entry (e.g. created before the queue
    existed). Idempotent; jobs already queued or leased are left alone."""
    n = 0
    for job in repo.list_active_jobs():
        if queue.state(job.job_id) is None:
            queue.enqueue(job.job_id)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Standalone process


async def _main() -> None:
    from .config import client_from_env, load_dotenv, queue_from_env, repo_from_env, worker_settings

    load_dotenv()
    repo = repo_from_env()
    queue = queue_from_env(repo)
    settings = worker_settings()
    if type(queue).__name__ == "InMemoryJobQueue":
        raise SystemExit("a separate worker process needs DRAMA_STORE=sqlite (the default)")
    from .media import media_from_env

    orchestrator = Orchestrator(repo, client_from_env(), checkpoints=queue, media=media_from_env())
    recover(repo, queue)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    workers = [
        JobWorker(repo, queue, orchestrator, lease_s=settings["lease_s"])
        for _ in range(max(settings["workers"], 1))
    ]
    tasks = [asyncio.create_task(w.run(stop)) for w in workers]
    await stop.wait()
    # Cancel in-flight jobs so they are released (requeued) instead of waiting for leases
    # to expire.
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
