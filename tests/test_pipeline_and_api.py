"""Orchestrator retry/termination (spec 8) and the HTTP contract (spec 9)."""

from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.llm import FixtureModelClient

from .conftest import run_pipeline


def break_timeline(output, attempt):
    output.shots[0].duration_ms += 500
    return output


def break_first_attempt_only(output, attempt):
    return break_timeline(output, attempt) if attempt == 0 else output


def test_step_retry_repairs_within_budget():
    client = FixtureModelClient(faults={"storyboard": break_first_attempt_only})
    job, _ = run_pipeline(client)
    assert job.status == "completed"
    assert job.stages["storyboard"].attempts == 2
    # Only the failing step was retried.
    assert [c.stage for c in client.calls].count("script") == 1
    assert client.calls[-2].feedback  # second storyboard attempt received the issues


def test_unrepairable_stage_returns_needs_revision_with_draft():
    job, repo = run_pipeline(FixtureModelClient(faults={"storyboard": break_timeline}))
    assert job.status == "needs_revision"
    assert job.stages["storyboard"].attempts == 3  # 1 + default budget of 2
    assert job.stages["review"].status == "pending"
    pkg = repo.get_package(job.package_id)
    assert pkg is not None and pkg.qa_report.hard_issues


def test_api_job_lifecycle_and_revision():
    client = TestClient(create_app())
    body = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
    created = client.post("/v1/drama-jobs", json=body)
    assert created.status_code == 202

    job = client.get(f"/v1/drama-jobs/{created.json()['job_id']}").json()
    assert job["status"] == "completed"
    pkg_id, rev_id = job["package_id"], job["revision_id"]

    pkg = client.get(f"/v1/drama-packages/{pkg_id}").json()
    assert len(pkg["episodes"]) == 3
    md = client.get(f"/v1/drama-packages/{pkg_id}", params={"format": "markdown"})
    assert md.text.startswith("# ")

    rev_url = f"/v1/drama-packages/{pkg_id}/revisions"
    stale = client.post(
        rev_url, json={"base_revision_id": "rev_old", "target_ids": ["ch_zhou"], "instruction": "x"}
    )
    assert stale.status_code == 409

    rev = client.post(
        rev_url,
        json={
            "base_revision_id": rev_id,
            "target_ids": ["ch_zhou"],
            "instruction": "第二集父亲的动机更可信，但不要提前揭示他的身份",
            "locked_ids": ["dl_01_01_01"],
        },
    )
    assert rev.status_code == 202
    assert "sc_02_01" in rev.json()["impacted_ids"]


def test_api_rejects_invalid_sources():
    client = TestClient(create_app())
    assert client.post("/v1/drama-jobs", json={"sources": []}).status_code == 422
    missing_asset = {"sources": [{"type": "image", "asset_id": "nope"}]}
    assert client.post("/v1/drama-jobs", json=missing_asset).status_code == 422
