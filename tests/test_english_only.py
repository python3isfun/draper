"""The app is English-only: US-set stories, no Chinese in code or UI, old stories archived."""

import asyncio
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.archive import ARCHIVE_REASON, archive_non_english, has_cjk
from drama_agent.fixtures.sample import SAMPLE_SEED, sample_request
from drama_agent.jobqueue import InMemoryJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, JobRequest
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository

from .conftest import run_pipeline, wait_job

ROOT = Path(__file__).resolve().parent.parent
# The design spec is the one file that stays in Chinese: it is the source document.
ALLOWED = {"drama_agent_service_v0_1.md"}
CHINESE_SEED = "\u4e00\u4e2a\u5e74\u8f7b\u4eba"  # a Chinese phrase, written as escapes


def test_no_chinese_in_code_ui_prompts_tests_or_docs():
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    offenders = []
    for rel in files:
        path = ROOT / rel
        if rel in ALLOWED or not path.is_file() or path.suffix in {".png", ".jpg", ".mp4"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [n for n, line in enumerate(text.splitlines(), 1) if has_cjk(line)]
        if lines:
            offenders.append(f"{rel}:{lines[:5]}")
    assert offenders == []


def test_sample_story_is_american_english(package):
    assert package.source_brief.language == "en-US"
    bible = package.story_bible
    assert bible.title == "The Last Month's Rent"
    assert {c.name for c in bible.characters} >= {"Maya Collins", "Frank Collins"}
    assert "Ohio" in package.set_sheet.locations[0].era_and_region


def test_prompts_ask_for_american_english_and_a_us_setting():
    client = FixtureModelClient()
    run_pipeline(client)
    instructions = client.calls[0].instructions
    assert "American English" in instructions and "present-day United States" in instructions


def test_api_accepts_only_english_jobs():
    with TestClient(create_app(workers=0)) as http:
        body = {"sources": [{"type": "text", "text": SAMPLE_SEED}]}
        created = http.post("/v1/drama-jobs", json=body).json()
        assert (
            http.get(f"/v1/drama-jobs/{created['job_id']}").json()["request"]["options"]["language"]
            == "en-US"
        )
        refused = http.post("/v1/drama-jobs", json={**body, "options": {"language": "zh-CN"}})
        assert refused.status_code == 422 and "American English" in refused.json()["detail"]


def chinese_job(job_id: str, status: str = "completed") -> Job:
    req = JobRequest.model_validate(
        {"sources": [{"type": "text", "text": CHINESE_SEED}], "options": {"language": "zh-CN"}}
    )
    return Job(job_id=job_id, request=req, status=status)


def test_chinese_stories_are_archived_not_deleted():
    repo = InMemoryRepository()
    repo.save_job(chinese_job("job_zh_done"))
    repo.save_job(chinese_job("job_zh_queued", status="queued"))
    repo.save_job(chinese_job("job_zh_running", status="running"))
    repo.save_job(Job(job_id="job_en", request=sample_request()))
    # An English job may quote other scripts ("Tokyo" in Japanese): its explicit en-US
    # language is trusted, so it is never archived.
    quoting = sample_request()
    quoting.sources[0].text += " Tokyo (\u6771\u4eac)."
    repo.save_job(Job(job_id="job_en_quoting", request=quoting))
    # An old job with no language set is judged by its package.
    job, prepo = run_pipeline()
    pkg = prepo.get_package(job.package_id)
    pkg.story_bible.title = CHINESE_SEED
    repo.save_package(pkg, published=True)
    old = job.model_copy(update={"job_id": "job_old_pkg"}, deep=True)
    old.request.options.language = None
    repo.save_job(old)

    assert archive_non_english(repo) == 3
    assert archive_non_english(repo) == 0  # idempotent
    done, queued = repo.get_job("job_zh_done"), repo.get_job("job_zh_queued")
    assert done.archived and done.archive_reason == ARCHIVE_REASON and done.status == "completed"
    assert queued.archived and queued.status == "cancelled"  # won't run any more
    assert repo.get_job("job_old_pkg").archived
    # Left for a later start, once it finishes (another process may still be saving it).
    assert not repo.get_job("job_zh_running").archived
    assert not repo.get_job("job_en").archived and not repo.get_job("job_en_quoting").archived
    assert {j.job_id for j in repo.list_archive_candidates()} == {"job_zh_running"}


def test_sqlite_lists_skip_archived_jobs_in_the_query(tmp_path):
    from drama_agent.store.sqlite import SqliteRepository

    repo = SqliteRepository(tmp_path / "t.db")
    for n in range(3):
        repo.save_job(chinese_job(f"job_zh_{n}"))
    repo.save_job(Job(job_id="job_en", request=sample_request()))
    assert archive_non_english(repo) == 3
    assert [j.job_id for j in repo.list_jobs(1, include_archived=False)] == ["job_en"]
    assert len(repo.list_jobs(10)) == 4 and repo.list_archive_candidates() == []


def test_archived_jobs_are_hidden_and_never_run():
    repo, queue = InMemoryRepository(), InMemoryJobQueue()
    repo.save_job(chinese_job("job_zh", status="needs_revision"))
    with TestClient(create_app(repo=repo, queue=queue, poll_s=0.01)) as http:
        job_id = http.post(
            "/v1/drama-jobs", json={"sources": [{"type": "text", "text": SAMPLE_SEED}]}
        ).json()["job_id"]
        wait_job(http, job_id)
        assert [j["job_id"] for j in http.get("/v1/drama-jobs").json()] == [job_id]
        listed = http.get("/v1/drama-jobs", params={"include_archived": True}).json()
        assert {j["job_id"] for j in listed} == {job_id, "job_zh"}
        archived = http.get("/v1/drama-jobs/job_zh").json()
        assert archived["archived"] is True
        assert http.post("/v1/drama-jobs/job_zh/retry").status_code == 409
        assert http.post("/v1/drama-jobs/job_zh/pause").status_code == 409

    orch = Orchestrator(repo, FixtureModelClient())
    assert asyncio.run(orch.run_job("job_zh")).status == "needs_revision"  # untouched
