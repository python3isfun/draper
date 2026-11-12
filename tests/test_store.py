"""Repository contract, run against every backend, plus SQLite persistence."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.config import repo_from_env
from drama_agent.fixtures.sample import SAMPLE_SEED, sample_request
from drama_agent.jobqueue import SqliteJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, QAReport, Revision
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SCHEMA_VERSION, SqliteRepository

from .conftest import wait_job


@pytest.fixture(params=["memory", "sqlite"])
def repo(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRepository()
    else:
        r = SqliteRepository(tmp_path / "drama.db")
        yield r
        r.close()


def draft_of(pkg, revision_id):
    d = pkg.model_copy(deep=True)
    d.revision_id = revision_id
    return d


# ---------------------------------------------------------------------------
# Contract (both backends)


def test_job_roundtrip_and_listing_order(repo):
    old = Job(job_id="job_old", request=sample_request())
    new = Job(job_id="job_new", request=sample_request())
    repo.save_job(old)
    repo.save_job(new)
    new.status = "running"
    repo.save_job(new)  # upsert

    assert repo.get_job("job_new").status == "running"
    assert repo.get_job("missing") is None
    assert [j.job_id for j in repo.list_jobs()] == ["job_new", "job_old"]
    assert [j.job_id for j in repo.list_jobs(limit=1)] == ["job_new"]


def test_package_revisions_are_immutable_and_published_wins(repo, package):
    repo.save_package(package, published=True)
    with pytest.raises(ValueError, match="immutable"):
        repo.save_package(package, published=True)

    repo.save_package(draft_of(package, "rev_draft"), published=False)
    assert repo.get_package(package.package_id).revision_id == package.revision_id
    assert repo.get_package(package.package_id, "rev_draft").revision_id == "rev_draft"
    assert repo.get_package("pkg_missing") is None
    assert repo.get_package(package.package_id, "rev_missing") is None

    repo.save_package(draft_of(package, "rev_newer"), published=True)
    assert repo.get_package(package.package_id).revision_id == "rev_newer"


def test_never_published_falls_back_to_newest_draft(repo, package):
    repo.save_package(draft_of(package, "rev_a"), published=False)
    repo.save_package(draft_of(package, "rev_b"), published=False)
    assert repo.get_package(package.package_id).revision_id == "rev_b"


def test_package_content_roundtrips_exactly(repo, package):
    repo.save_package(package, published=True)
    assert repo.get_package(package.package_id) == package


def test_revision_report_and_asset_roundtrip(repo):
    rev = Revision(revision_id="rev_1", package_id="pkg_1", base_revision_id=None)
    repo.save_revision(rev)
    assert repo.get_revision("rev_1") == rev
    repo.save_evaluation_report("pkg_1", "rev_1", QAReport())
    repo.save_source_asset("asset_1", {"type": "image", "name": "photo.png"})
    assert repo.get_source_asset("asset_1") == {"type": "image", "name": "photo.png"}
    assert repo.get_source_asset("nope") is None


def test_list_active_jobs(repo):
    repo.save_job(Job(job_id="job_run", request=sample_request(), status="running"))
    repo.save_job(Job(job_id="job_queued", request=sample_request()))
    repo.save_job(Job(job_id="job_done", request=sample_request(), status="completed"))
    assert [j.job_id for j in repo.list_active_jobs()] == ["job_run", "job_queued"]


def test_full_pipeline_against_backend(repo):
    job = Job(job_id="job_e2e", request=sample_request())
    repo.save_job(job)
    done = asyncio.run(Orchestrator(repo, FixtureModelClient()).run_job("job_e2e"))
    stored = repo.get_job("job_e2e")
    assert done.status == stored.status == "completed"
    assert repo.get_package(stored.package_id).revision_id == stored.revision_id


# ---------------------------------------------------------------------------
# SQLite specifics


def test_sqlite_survives_reopen(tmp_path, package):
    path = tmp_path / "nested" / "drama.db"  # parent dirs are created
    first = SqliteRepository(path)
    first.save_job(Job(job_id="job_1", request=sample_request(), status="completed"))
    first.save_package(package, published=True)
    first.close()

    second = SqliteRepository(path)
    assert second.get_job("job_1").status == "completed"
    assert second.get_package(package.package_id) == package
    second.close()


def test_sqlite_rejects_newer_schema(tmp_path):
    path = tmp_path / "drama.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="schema"):
        SqliteRepository(path)


def test_api_results_persist_across_server_restart(tmp_path):
    path = tmp_path / "drama.db"
    body = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}

    def app_on(path):
        repo = SqliteRepository(path)
        return TestClient(create_app(repo=repo, queue=SqliteJobQueue(repo), poll_s=0.01))

    with app_on(path) as app1:
        job_id = app1.post("/v1/drama-jobs", json=body).json()["job_id"]
        pkg_id = wait_job(app1, job_id)["package_id"]

    with app_on(path) as app2:  # "restarted" server
        assert app2.get(f"/v1/drama-jobs/{job_id}").json()["status"] == "completed"
        assert len(app2.get(f"/v1/drama-packages/{pkg_id}").json()["episodes"]) == 3
        assert [j["job_id"] for j in app2.get("/v1/drama-jobs").json()] == [job_id]


def test_repo_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DRAMA_STORE", raising=False)
    monkeypatch.setenv("DRAMA_DB_PATH", str(tmp_path / "x.db"))
    repo = repo_from_env()
    assert isinstance(repo, SqliteRepository) and (tmp_path / "x.db").exists()
    repo.close()

    monkeypatch.setenv("DRAMA_STORE", "memory")
    assert isinstance(repo_from_env(), InMemoryRepository)
    monkeypatch.setenv("DRAMA_STORE", "redis")
    with pytest.raises(RuntimeError):
        repo_from_env()
