"""Adaptation mode: the sources are the story to adapt, and must_keep items are kept."""

import asyncio

from drama_agent.fixtures import sample
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, JobRequest
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.pipeline.steps import ADAPTATION_RULES, ADAPTATION_RULES_VERSION
from drama_agent.store import InMemoryRepository

KEEP = ["Ryder is a woman", "The last episode echoes the first line"]


def adaptation_request(must_keep=KEEP) -> JobRequest:
    raw = sample.sample_request().model_dump(mode="json")
    raw["sources"][0] |= {"type": "script", "use_as": "plot_reference", "must_keep": must_keep}
    raw["options"] |= {"reference_mode": "adaptation", "content_rating": "mature"}
    return JobRequest.model_validate(raw)


def run(request: JobRequest, client: FixtureModelClient):
    repo = InMemoryRepository()
    repo.save_job(Job(job_id="job_a", request=request))
    job = asyncio.run(Orchestrator(repo, client).run_job("job_a"))
    return job, repo


def keeps_constraints(output, attempt):
    output.user_constraints += [f"  {k.upper()} " for k in KEEP]  # case and spacing differ
    return output


def test_adaptation_rules_go_to_every_step():
    client = FixtureModelClient(faults={"ingest": keeps_constraints})
    job, repo = run(adaptation_request(), client)
    assert job.status == "completed", job.failure_reason
    assert {c.stage for c in client.calls} >= {"ingest", "outline", "script", "review"}
    assert all(ADAPTATION_RULES in c.instructions for c in client.calls)
    pkg = repo.get_package(job.package_id)
    assert pkg.provenance.prompt_versions["adaptation_rules"] == ADAPTATION_RULES_VERSION


def test_inspiration_mode_gets_no_adaptation_rules():
    client = FixtureModelClient()
    job, repo = run(sample.sample_request(), client)
    assert job.status == "completed"
    assert not any("Adaptation mode" in c.instructions for c in client.calls)
    assert "adaptation_rules" not in repo.get_package(job.package_id).provenance.prompt_versions


def test_missing_must_keep_is_retried_with_feedback():
    def keeps_on_retry(output, attempt):
        return keeps_constraints(output, attempt) if attempt else output

    client = FixtureModelClient(faults={"ingest": keeps_on_retry})
    job, _ = run(adaptation_request(), client)
    assert job.status == "completed", job.failure_reason
    assert job.stages["ingest"].attempts == 2
    retry = [c for c in client.calls if c.stage == "ingest"][1]
    assert {i.check for i in retry.feedback} == {"must_keep"}
    assert any("Ryder is a woman" in i.evidence for i in retry.feedback)


def test_must_keep_never_kept_ends_needs_revision():
    job, _ = run(adaptation_request(), FixtureModelClient())
    assert job.status == "needs_revision"
    assert job.stages["ingest"].status != "passed"
