"""Retrying needs_revision / failed jobs: resume from the checkpoint, new revision."""

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.jobqueue import InMemoryJobQueue, SqliteJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}


def break_timeline(output, attempt):
    output.shots[0].duration_ms += 500
    return output


def boom(output, attempt):
    raise ConnectionError("gateway down")


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    if request.param == "memory":
        q = InMemoryJobQueue(max_attempts=1)
        yield InMemoryRepository(), q
    else:
        repo = SqliteRepository(tmp_path / "retry.db")
        yield repo, SqliteJobQueue(repo, max_attempts=1)
        repo.close()


def api(backend, client):
    repo, queue = backend
    app = create_app(repo=repo, client=client, queue=queue, poll_s=0.01, retry_base_s=0)
    return TestClient(app)


def stage_calls(client):
    return [c.stage for c in client.calls]


def test_retry_needs_revision_resumes_and_publishes_new_revision(backend):
    client = FixtureModelClient(faults={"storyboard": break_timeline})
    with api(backend, client) as http:
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        first = wait_job(http, job_id)
        assert first["status"] == "needs_revision"
        draft_rev = first["revision_id"]

        client.faults.clear()  # e.g. the bug that caused it has been fixed
        queued = http.post(f"/v1/drama-jobs/{job_id}/retry")
        assert queued.status_code == 202
        body = queued.json()
        assert body["status"] == "queued" and body["package_id"] is None
        assert body["failure_reason"] == "retry requested; keeping ingest, outline, script"
        assert body["stages"]["storyboard"] == {
            "status": "pending",
            "attempts": 0,
            "issues": [],
            "error": None,
        }

        done = wait_job(http, job_id)
        assert done["status"] == "completed"
        assert done["revision_id"] != draft_rev  # immutable draft kept; new revision used
        pkg = http.get(f"/v1/drama-packages/{done['package_id']}").json()
        assert pkg["revision_id"] == done["revision_id"]  # published one is the default
        draft = http.get(
            f"/v1/drama-packages/{done['package_id']}", params={"revision_id": draft_rev}
        ).json()
        assert draft["qa_report"]["hard_issues"]  # the failed draft is still inspectable

    calls = stage_calls(client)
    assert calls.count("ingest") == calls.count("outline") == calls.count("script") == 1
    assert calls.count("storyboard") == 4  # 3 failing attempts + 1 on retry


def test_retry_failed_job_with_exhausted_queue_entry(backend):
    client = FixtureModelClient(faults={"outline": boom})
    with api(backend, client) as http:
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        failed = wait_job(http, job_id)
        assert failed["status"] == "failed"  # max_attempts=1, so the entry is dead
        assert "gateway down" in failed["failure_reason"]

        client.faults.clear()
        assert http.post(f"/v1/drama-jobs/{job_id}/retry").status_code == 202
        assert wait_job(http, job_id)["status"] == "completed"
    assert stage_calls(client).count("ingest") == 1  # ingest passed before the failure


def test_retry_rejects_unfinished_and_unknown_jobs(backend):
    with api(backend, FixtureModelClient()) as http:
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        assert wait_job(http, job_id)["status"] == "completed"
        conflict = http.post(f"/v1/drama-jobs/{job_id}/retry")
        assert conflict.status_code == 409 and "completed" in conflict.json()["detail"]
        assert http.post("/v1/drama-jobs/job_nope/retry").status_code == 404
