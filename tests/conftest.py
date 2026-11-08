import asyncio

import pytest

from drama_agent.fixtures import sample
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, ProductionPackage
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository


def run_pipeline(client: FixtureModelClient | None = None) -> tuple[Job, InMemoryRepository]:
    repo = InMemoryRepository()
    orch = Orchestrator(repo, client or FixtureModelClient())
    job = Job(job_id="job_test", request=sample.sample_request())
    repo.save_job(job)
    return asyncio.run(orch.run_job(job.job_id)), repo


@pytest.fixture
def package() -> ProductionPackage:
    """A fresh, hard-check-passing package produced by the real pipeline."""
    job, repo = run_pipeline()
    assert job.status == "completed"
    pkg = repo.get_package(job.package_id)
    assert pkg is not None
    return pkg


@pytest.fixture
def options():
    return sample.sample_request().options


def wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    """Poll the API until the job leaves queued/running (workers run in the app lifespan)."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/v1/drama-jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} still {job['status']} after {timeout}s")
        time.sleep(0.02)
