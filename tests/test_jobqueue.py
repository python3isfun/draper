"""Durable job queue and worker: leases, retries, checkpoints, crash recovery."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED, sample_request
from drama_agent.jobqueue import InMemoryJobQueue, SqliteJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository
from drama_agent.worker import JobWorker, recover

from .conftest import wait_job


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """(repo, queue) pair for each backend."""
    if request.param == "memory":
        yield InMemoryRepository(), InMemoryJobQueue(max_attempts=3)
    else:
        repo = SqliteRepository(tmp_path / "q.db")
        yield repo, SqliteJobQueue(repo, max_attempts=3)
        repo.close()


def add_job(repo, queue, job_id="job_1"):
    repo.save_job(Job(job_id=job_id, request=sample_request()))
    queue.enqueue(job_id)


# ---------------------------------------------------------------------------
# Queue contract


def test_claim_order_ownership_and_completion(store):
    _, q = store
    q.enqueue("a")
    time.sleep(0.001)
    q.enqueue("b")
    q.enqueue("a")  # idempotent

    first = q.claim("w1", lease_s=30)
    assert (first.job_id, first.attempt, first.recovered) == ("a", 1, False)
    assert q.claim("w2", lease_s=30).job_id == "b"
    assert q.claim("w3", lease_s=30) is None  # both leased

    assert q.heartbeat("a", "w1", 30) is True
    assert q.heartbeat("a", "w2", 30) is False  # not the owner
    q.complete("a", "w1")
    assert q.state("a") == "done"


def test_expired_lease_is_recovered(store):
    _, q = store
    q.enqueue("a")
    q.claim("dead-worker", lease_s=0.01)
    time.sleep(0.02)
    claim = q.claim("w2", lease_s=30)
    assert claim.job_id == "a" and claim.recovered and claim.attempt == 2
    assert q.heartbeat("a", "dead-worker", 30) is False


def test_retry_backoff_and_exhaustion(store):
    _, q = store
    q.enqueue("a")
    q.claim("w", 30)
    assert q.retry("a", "w", "boom", delay_s=0.05) is True
    assert q.claim("w", 30) is None  # still backing off
    time.sleep(0.06)
    assert q.claim("w", 30).attempt == 2
    assert q.retry("a", "w", "boom", delay_s=0) is True
    assert q.claim("w", 30).attempt == 3
    assert q.retry("a", "w", "boom", delay_s=0) is False  # 3 of 3 used
    assert q.state("a") == "dead"


def test_crash_on_last_attempt_yields_exhausted_claim(store):
    _, q = store
    q.enqueue("a")
    for _ in range(3):
        q.claim("dead", lease_s=0.001)
        time.sleep(0.005)
    claim = q.claim("w", 30)
    assert claim.exhausted and q.state("a") == "dead"


def test_release_does_not_spend_an_attempt(store):
    _, q = store
    q.enqueue("a")
    q.claim("w", 30)
    q.release("a", "w")
    assert q.state("a") == "queued"
    assert q.claim("w", 30).attempt == 1


def test_checkpoint_roundtrip(store):
    _, q = store
    q.enqueue("a")
    assert q.load_checkpoint("a") is None
    q.save_checkpoint("a", '{"x": 1}')
    assert q.load_checkpoint("a") == '{"x": 1}'


def test_two_processes_never_double_claim(tmp_path):
    """Separate connections to one file behave like separate worker processes."""
    path = tmp_path / "shared.db"
    repo_a, repo_b = SqliteRepository(path), SqliteRepository(path)
    qa, qb = SqliteJobQueue(repo_a), SqliteJobQueue(repo_b)
    for i in range(20):
        qa.enqueue(f"job_{i:02d}")
    claimed = []
    for _ in range(12):
        for q, w in ((qa, "A"), (qb, "B")):
            if c := q.claim(w, 30):
                claimed.append(c.job_id)
    assert len(claimed) == 20 and len(set(claimed)) == 20


# ---------------------------------------------------------------------------
# Worker behaviour


def make_worker(repo, queue, client=None, **kw):
    orch = Orchestrator(repo, client or FixtureModelClient(), checkpoints=queue)
    return JobWorker(repo, queue, orch, retry_base_s=0, **kw)


def test_worker_completes_job(store):
    repo, q = store
    add_job(repo, q)
    assert asyncio.run(make_worker(repo, q).run_once()) is True
    job = repo.get_job("job_1")
    assert job.status == "completed" and job.attempts == 1
    assert q.state("job_1") == "done"


def test_transient_failure_is_retried_then_succeeds(store):
    repo, q = store
    add_job(repo, q)

    def flaky(output, attempt):
        flaky.calls += 1
        if flaky.calls == 1:
            raise ConnectionError("gateway timeout")
        return output

    flaky.calls = 0
    client = FixtureModelClient(faults={"storyboard": flaky})
    worker = make_worker(repo, q, client)

    asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    assert job.status == "queued"
    assert "attempt 1/3 failed: ConnectionError: gateway timeout" in job.failure_reason

    asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    assert job.status == "completed" and job.attempts == 2 and job.failure_reason is None
    # Resumed from the checkpoint: earlier steps ran once across both attempts.
    stages = [c.stage for c in client.calls]
    assert stages.count("ingest") == stages.count("script") == 1
    assert stages.count("storyboard") == 2


def test_permanent_failure_gives_up_after_max_attempts(store):
    repo, q = store
    add_job(repo, q)

    def always(output, attempt):
        raise ConnectionError("down")

    worker = make_worker(repo, q, FixtureModelClient(faults={"ingest": always}))
    for _ in range(3):
        asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    assert job.status == "failed"
    assert job.failure_reason.startswith("failed after 3 attempts: ConnectionError: down")
    assert q.state("job_1") == "dead"
    assert asyncio.run(worker.run_once()) is False


def test_needs_revision_is_final_not_retried(store):
    repo, q = store
    add_job(repo, q)

    def broken(output, attempt):
        output.shots[0].duration_ms += 500
        return output

    worker = make_worker(repo, q, FixtureModelClient(faults={"storyboard": broken}))
    asyncio.run(worker.run_once())
    assert repo.get_job("job_1").status == "needs_revision"
    assert q.state("job_1") == "done"


class SimulatedCrash(BaseException):
    """BaseException so the orchestrator's error handling can't catch it, like a kill."""


def test_crashed_worker_job_resumes_from_checkpoint(store):
    repo, q = store
    add_job(repo, q)

    def die(output, attempt):
        raise SimulatedCrash

    # A worker claims the job with a short lease and dies during the storyboard step.
    q.claim("doomed", lease_s=0.01)
    crashing = Orchestrator(repo, FixtureModelClient(faults={"storyboard": die}), checkpoints=q)
    with pytest.raises(SimulatedCrash):
        asyncio.run(crashing.run_job("job_1"))
    assert repo.get_job("job_1").status == "running"  # left mid-flight
    time.sleep(0.02)  # lease expires

    client = FixtureModelClient()
    asyncio.run(make_worker(repo, q, client).run_once())
    job = repo.get_job("job_1")
    assert job.status == "completed" and job.attempts == 2
    assert [c.stage for c in client.calls] == ["storyboard", "review"]
    assert all(s.status == "passed" for s in job.stages.values())


def test_crash_after_package_saved_does_not_break_resume(store):
    """_finish is idempotent: the package may exist before the job is marked done."""
    repo, q = store
    add_job(repo, q)
    worker = make_worker(repo, q)
    asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    job.status = "running"  # pretend we crashed after saving the package
    repo.save_job(job)
    asyncio.run(worker.orchestrator.run_job("job_1"))
    assert repo.get_job("job_1").status == "completed"


class BlockingClient(FixtureModelClient):
    """Hangs on one stage until cancelled, like a slow live model call."""

    def __init__(self, stage: str):
        super().__init__()
        self.block_stage = stage
        self.started = asyncio.Event()

    async def generate(self, request, output_model):
        if request.stage == self.block_stage:
            self.started.set()
            await asyncio.Event().wait()
        return await super().generate(request, output_model)


def test_graceful_shutdown_requeues_without_spending_attempt(store):
    repo, q = store
    add_job(repo, q)
    client = BlockingClient("script")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(make_worker(repo, q, client, poll_s=0.01).run(stop))
        await asyncio.wait_for(client.started.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    job = repo.get_job("job_1")
    assert job.status == "queued" and "shut down" in job.failure_reason
    assert job.stages["script"].status == "pending"
    assert q.state("job_1") == "queued"
    assert q.claim("w", 30).attempt == 1  # the interrupted run was not counted


def test_lost_lease_stops_work(store):
    repo, q = store
    add_job(repo, q)
    client = BlockingClient("outline")
    worker = make_worker(repo, q, client, lease_s=0.03)
    q_heartbeat = q.heartbeat
    q.heartbeat = lambda *a: False  # another worker took over

    asyncio.run(asyncio.wait_for(worker.run_once(), 5))
    q.heartbeat = q_heartbeat
    assert repo.get_job("job_1").status == "running"  # left for the new owner
    assert q.state("job_1") == "leased"


def test_recover_enqueues_orphans_only(store):
    repo, q = store
    repo.save_job(Job(job_id="orphan", request=sample_request(), status="running"))
    repo.save_job(Job(job_id="done", request=sample_request(), status="completed"))
    add_job(repo, q, "already_queued")
    assert recover(repo, q) == 1
    assert q.state("orphan") == "queued" and q.state("done") is None


# ---------------------------------------------------------------------------
# API integration


def test_job_queued_while_api_down_runs_after_start(tmp_path):
    path = tmp_path / "api.db"
    body = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}

    repo = SqliteRepository(path)
    with TestClient(create_app(repo=repo, queue=SqliteJobQueue(repo), workers=0)) as api:
        job_id = api.post("/v1/drama-jobs", json=body).json()["job_id"]
        assert api.get(f"/v1/drama-jobs/{job_id}").json()["status"] == "queued"
    repo.close()

    repo = SqliteRepository(path)
    app = create_app(repo=repo, queue=SqliteJobQueue(repo), workers=1, poll_s=0.01)
    with TestClient(app) as api:
        assert wait_job(api, job_id)["status"] == "completed"
    repo.close()
