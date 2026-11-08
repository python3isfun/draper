"""HTTP API (spec section 9)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import client_from_env, load_dotenv, repo_from_env
from ..llm import FixtureModelClient, ModelClient
from ..models import Job, JobRequest, ProductionPackage, Revision, RevisionRequest
from ..pipeline.impact import analyze_impact
from ..pipeline.orchestrator import Orchestrator, new_id
from ..render import render_markdown
from ..store import InMemoryRepository, Repository

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class JobCreated(BaseModel):
    job_id: str
    status: str


class JobSummary(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    seed: str
    package_id: str | None
    revision_id: str | None


class Meta(BaseModel):
    model_id: str
    client: str


def create_app(repo: Repository | None = None, client: ModelClient | None = None) -> FastAPI:
    repo = repo or InMemoryRepository()
    orchestrator = Orchestrator(repo, client or FixtureModelClient())
    app = FastAPI(title="Drama Agent", version="0.1.0")
    app.state.repo = repo
    app.state.orchestrator = orchestrator

    @app.get("/v1/meta", response_model=Meta)
    async def meta() -> Meta:
        c = orchestrator.client
        return Meta(model_id=c.model_id, client=type(c).__name__)

    @app.get("/v1/drama-jobs", response_model=list[JobSummary])
    async def list_jobs(limit: int = Query(50, ge=1, le=200)) -> list[JobSummary]:
        out = []
        for j in repo.list_jobs(limit):
            first = j.request.sources[0]
            seed = first.text or f"[{first.type}] {first.asset_id or ''}"
            out.append(
                JobSummary(
                    job_id=j.job_id,
                    status=j.status,
                    created_at=j.created_at,
                    seed=seed[:120],
                    package_id=j.package_id,
                    revision_id=j.revision_id,
                )
            )
        return out

    @app.post("/v1/drama-jobs", status_code=202, response_model=JobCreated)
    async def create_job(req: JobRequest, background: BackgroundTasks) -> JobCreated:
        for src in req.sources:
            if src.asset_id and repo.get_source_asset(src.asset_id) is None:
                raise HTTPException(422, f"unknown asset_id {src.asset_id}")
        job = Job(job_id=new_id("job"), request=req)
        repo.save_job(job)
        background.add_task(orchestrator.run_job, job.job_id)
        return JobCreated(job_id=job.job_id, status=job.status)

    @app.get("/v1/drama-jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str) -> Job:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.get("/v1/drama-packages/{package_id}", response_model=None)
    async def get_package(
        package_id: str,
        revision_id: str | None = None,
        format: Literal["json", "markdown"] = Query("json"),
    ) -> ProductionPackage | PlainTextResponse:
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        if format == "markdown":
            return PlainTextResponse(render_markdown(pkg), media_type="text/markdown")
        return pkg

    @app.post("/v1/drama-packages/{package_id}/revisions", status_code=202, response_model=Revision)
    async def create_revision(package_id: str, req: RevisionRequest) -> Revision:
        pkg = repo.get_package(package_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        if req.base_revision_id != pkg.revision_id:
            # Prevents overwriting a newer edit.
            raise HTTPException(409, f"base_revision_id is stale; latest is {pkg.revision_id}")
        impact = analyze_impact(pkg, req.target_ids, req.locked_ids, req.visual_change)
        if impact.unknown_targets:
            raise HTTPException(422, f"unknown target_ids: {impact.unknown_targets}")

        rev = Revision(
            revision_id=new_id("rev"),
            package_id=package_id,
            base_revision_id=pkg.revision_id,
            request=req,
            impacted_ids=impact.impacted_ids,
            stale_requirement_ids=impact.stale_requirement_ids,
            conflicts=[f"target {c} is locked" for c in impact.conflicts],
            locked_in_scope=impact.locked_in_scope,
            status="needs_revision" if impact.conflicts else "queued",
        )
        repo.save_revision(rev)
        # TODO: run the scoped regeneration worker (re-run only impacted steps with
        # locked_ids enforced via check_locks), then publish a new package revision.
        return rev

    # Web UI. Mounted last so it never shadows the /v1 API routes.
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app


def create_app_from_env() -> FastAPI:
    """Entry point for uvicorn: reads .env and picks the model client from DRAMA_MODEL_CLIENT."""
    load_dotenv()
    repo = repo_from_env()
    # Background tasks die with the process; jobs a previous run left mid-flight would
    # otherwise show 'running' forever.
    interrupted = repo.fail_interrupted_jobs("interrupted by server restart")
    if interrupted:
        log.warning("marked %d interrupted jobs as failed: %s", len(interrupted), interrupted)
    return create_app(repo=repo, client=client_from_env())


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazy `app` for `uvicorn drama_agent.api.app:app`.

    Built on first access so importing this module (e.g. in tests) never reads .env or
    constructs a paid model client.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = create_app_from_env()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
