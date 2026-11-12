"""Live trace (agent steps + model calls) and pause/resume."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED, sample_request
from drama_agent.jobqueue import InMemoryJobQueue, SqliteJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, LLMCall, TraceEvent
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SCHEMA_VERSION, SqliteRepository
from drama_agent.worker import JobWorker, pause_job, resume_job

from .conftest import run_pipeline

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
STAGES = ["ingest", "outline", "script", "cast", "sets", "props", "storyboard", "review"]
SHEETS = {"cast", "sets", "props"}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRepository(), InMemoryJobQueue()
    else:
        repo = SqliteRepository(tmp_path / "t.db")
        yield repo, SqliteJobQueue(repo)
        repo.close()


def kinds(repo, job_id):
    return [e.kind for e in repo.list_events(job_id)]


# ---------------------------------------------------------------------------
# Trace content


def test_full_run_traces_every_step_and_call():
    job, repo = run_pipeline()
    events = repo.list_events(job.job_id)
    assert events[0].kind == "job_started" and events[-1].kind == "job_completed"
    for stage in STAGES:  # every step starts once and passes once
        assert [e.kind for e in events if e.stage == stage] == ["stage_started", "stage_passed"]
    calls = repo.list_llm_calls(job.job_id)
    order = [c.stage for c in calls]
    # The design sheets run concurrently, between script and storyboard.
    assert order[:3] == STAGES[:3] and order[-2:] == STAGES[-2:]
    assert set(order[3:6]) == SHEETS
    assert all(c.status == "succeeded" and c.finished_at for c in calls)
    assert all(c.commentary.startswith("Offline sample") for c in calls)
    full = repo.get_llm_call(calls[1].call_id)
    assert '"story_bible"' in full.output and calls[1].output == ""  # list omits bodies
    assert calls[1].output_chars == len(full.output)


def test_gate_failures_and_cleanup_are_traced():
    def broken_once(output, attempt):
        if attempt == 0:
            output.shots[0].duration_ms += 500
        return output

    def leaky(output, attempt):
        output.episodes[0].scenes[0].beats[0].action = "Shot [0,15000)ms: The door opens."
        return output

    job, repo = run_pipeline(
        FixtureModelClient(faults={"storyboard": broken_once, "script": leaky})
    )
    events = repo.list_events(job.job_id)
    gate = next(e for e in events if e.kind == "gate_issues")
    assert gate.stage == "storyboard" and gate.attempt == 0 and gate.data["issues"]
    retry = next(e for e in events if e.kind == "attempt_started")
    assert retry.message.startswith("Attempt 2 of 3")
    assert any(e.kind == "normalized" and e.stage == "script" for e in events)
    assert [c.attempt for c in repo.list_llm_calls(job.job_id) if c.stage == "storyboard"] == [0, 1]


def test_failed_call_is_recorded_with_its_error():
    def boom(output, attempt):
        raise ConnectionError("gateway down")

    job, repo = run_pipeline(FixtureModelClient(faults={"outline": boom}))
    [failed] = [c for c in repo.list_llm_calls(job.job_id) if c.stage == "outline"]
    assert failed.status == "failed" and "gateway down" in failed.error
    assert kinds(repo, job.job_id)[-1] == "job_failed"


# ---------------------------------------------------------------------------
# Storage contract


def test_events_are_ordered_and_incremental(store):
    repo, _ = store
    for i in range(3):
        repo.add_event(TraceEvent(job_id="j", kind="k", message=f"m{i}"))
    repo.add_event(TraceEvent(job_id="other", kind="k", message="x"))
    events = repo.list_events("j")
    assert [e.message for e in events] == ["m0", "m1", "m2"]
    assert [e.message for e in repo.list_events("j", after_seq=events[0].seq)] == ["m1", "m2"]


def test_llm_call_upsert_and_bodies(store):
    repo, _ = store
    call = LLMCall(
        call_id="c1",
        job_id="j",
        stage="script",
        attempt=0,
        model="m",
        request={"messages": [{"role": "user", "content": "hi"}]},
    )
    repo.save_llm_call(call)
    call.output, call.commentary, call.status = '{"a": 1}', "Writing episode one", "succeeded"
    repo.save_llm_call(call)
    [listed] = repo.list_llm_calls("j")
    assert listed.status == "succeeded" and listed.commentary == "Writing episode one"
    assert listed.output == "" and listed.request == {} and listed.output_chars == 8
    full = repo.get_llm_call("c1")
    assert full.output == '{"a": 1}' and full.request["messages"][0]["content"] == "hi"
    assert repo.get_llm_call("nope") is None


def test_v2_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE job_queue (job_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, "
        "enqueued_at REAL NOT NULL, lease_owner TEXT, lease_expires_at REAL, "
        "last_error TEXT, checkpoint TEXT);"
        "INSERT INTO job_queue VALUES ('old', 'queued', 0, 0, 0, NULL, NULL, NULL, NULL);"
        "PRAGMA user_version=2;"
    )
    conn.close()
    repo = SqliteRepository(path)
    q = SqliteJobQueue(repo)
    assert q.request_pause("old") == "paused"  # uses the new column/state
    assert repo._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    repo.close()


# ---------------------------------------------------------------------------
# Pause / resume


def make_worker(repo, queue, client=None):
    orch = Orchestrator(repo, client or FixtureModelClient(), checkpoints=queue)
    return JobWorker(repo, queue, orch, retry_base_s=0)


def add_job(repo, queue, job_id="job_1"):
    repo.save_job(Job(job_id=job_id, request=sample_request()))
    queue.enqueue(job_id)


def test_pause_before_start_and_resume(store):
    repo, q = store
    add_job(repo, q)
    job = pause_job(repo, q, "job_1")
    assert job.status == "paused" and repo.get_job("job_1").status == "paused"
    worker = make_worker(repo, q)
    assert asyncio.run(worker.run_once()) is False  # paused jobs aren't claimed

    resume_job(repo, q, "job_1")
    assert asyncio.run(worker.run_once()) is True
    assert repo.get_job("job_1").status == "completed"
    assert kinds(repo, "job_1")[:2] == ["paused", "resumed"]


def test_pause_mid_run_stops_at_the_next_safe_point(store):
    repo, q = store
    add_job(repo, q)

    def press_pause(output, attempt):  # the user clicks Pause while the script call runs
        pause_job(repo, q, "job_1")
        return output

    client = FixtureModelClient(faults={"script": press_pause})
    asyncio.run(make_worker(repo, q, client).run_once())

    job = repo.get_job("job_1")
    assert job.status == "paused"
    # The in-flight script call finished and was kept; nothing after it ran.
    assert [job.stages[s].status for s in STAGES] == ["passed"] * 3 + ["pending"] * 5
    assert q.state("job_1") == "paused" and not q.pause_requested("job_1")
    assert [k for k in kinds(repo, "job_1") if "pause" in k] == ["pause_requested", "paused"]

    client.faults.clear()
    resume_job(repo, q, "job_1")
    asyncio.run(make_worker(repo, q, client).run_once())
    job = repo.get_job("job_1")
    assert job.status == "completed"
    assert job.attempts == 1  # the paused run didn't use up an attempt
    calls = [c.stage for c in client.calls]
    assert calls.count("ingest") == calls.count("script") == 1  # resumed, not restarted


def test_pause_and_resume_reject_wrong_states(store):
    repo, q = store
    add_job(repo, q)
    with pytest.raises(ValueError, match="only paused"):
        resume_job(repo, q, "job_1")
    asyncio.run(make_worker(repo, q).run_once())  # completes
    with pytest.raises(ValueError, match="only queued or running"):
        pause_job(repo, q, "job_1")
    with pytest.raises(KeyError):
        pause_job(repo, q, "nope")


# ---------------------------------------------------------------------------
# API


def test_api_trace_call_and_pause_endpoints():
    with TestClient(create_app(workers=0)) as http:  # no workers: the job stays queued
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        paused = http.post(f"/v1/drama-jobs/{job_id}/pause")
        assert paused.status_code == 202 and paused.json()["status"] == "paused"
        assert http.post(f"/v1/drama-jobs/{job_id}/pause").status_code == 409
        resumed = http.post(f"/v1/drama-jobs/{job_id}/resume")
        assert resumed.status_code == 202 and resumed.json()["status"] == "queued"
        assert http.post(f"/v1/drama-jobs/{job_id}/resume").status_code == 409
        assert http.post("/v1/drama-jobs/nope/pause").status_code == 404

        trace = http.get(f"/v1/drama-jobs/{job_id}/trace").json()
        assert [e["kind"] for e in trace["events"]] == ["paused", "resumed"]
        newer = http.get(f"/v1/drama-jobs/{job_id}/trace", params={"after_seq": trace["next_seq"]})
        assert newer.json()["events"] == [] and newer.json()["next_seq"] == trace["next_seq"]
        assert http.get("/v1/drama-jobs/nope/trace").status_code == 404
        assert http.get("/v1/llm-calls/nope").status_code == 404


def test_api_trace_after_a_completed_run():
    from .conftest import wait_job

    with TestClient(create_app(poll_s=0.01)) as http:
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        wait_job(http, job_id)
        trace = http.get(f"/v1/drama-jobs/{job_id}/trace").json()
        assert trace["events"][-1]["kind"] == "job_completed"
        first = trace["calls"][0]["call"]
        assert first["stage"] == "ingest"
        assert first["request"] == {}  # the live view omits request bodies
        full = http.get(f"/v1/llm-calls/{first['call_id']}").json()
        assert full["output"] and full["request"] == {"stage": "ingest"}
