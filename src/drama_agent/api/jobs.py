"""Job and upload routes: creating, listing, pausing, resuming and retrying drama jobs;
source uploads; the trace."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..media import MediaError, UploadTooLarge
from ..models import (
    Job,
    JobRequest,
    LLMCall,
    SourceAsset,
)
from ..pipeline.orchestrator import new_id
from ..worker import pause_job, resume_job, retry_job
from .schemas import (
    OUTPUT_TAIL_CHARS,
    JobCreated,
    JobSummary,
    LLMCallView,
    Trace,
    _seed_label,
)


def register(app: FastAPI, ctx: Any) -> None:
    """Add these routes; `ctx` holds what create_app shares (repo, media, runners, helpers)."""
    _file = ctx._file
    media = ctx.media
    queue = ctx.queue
    repo = ctx.repo
    wakeup = ctx.wakeup

    @app.get("/v1/drama-jobs", response_model=list[JobSummary])
    async def list_jobs(
        limit: int = Query(50, ge=1, le=200), include_archived: bool = False
    ) -> list[JobSummary]:
        out = []
        jobs = repo.list_jobs(limit, include_archived=include_archived)
        # One batched lookup for the file names of media-only jobs (not one per job).
        firsts = [j.request.sources[0].asset_id for j in jobs]
        assets = repo.get_source_assets([a for a in firsts if a])
        for j in jobs:
            seed = _seed_label(j, assets)
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
    async def create_job(req: JobRequest) -> JobCreated:
        if (req.options.language or "en-US").lower() not in ("en-us", "en"):
            raise HTTPException(
                422, "stories are written in American English: language must be en-US"
            )
        req.options.language = "en-US"
        n_media = sum(1 for src in req.sources if src.type in ("image", "video"))
        if n_media > media.limits.max_media_sources:
            raise HTTPException(
                422, f"at most {media.limits.max_media_sources} images and videos per job"
            )
        for i, src in enumerate(req.sources):
            if not src.asset_id:
                continue
            raw = repo.get_source_asset(src.asset_id)
            if raw is None:
                raise HTTPException(422, f"unknown asset_id {src.asset_id}")
            kind = raw.get("kind")
            if src.type in ("image", "video") and kind != src.type:
                raise HTTPException(
                    422, f"sources[{i}] is type {src.type} but {src.asset_id} is a {kind}"
                )
        job = Job(job_id=new_id("job"), request=req)
        repo.save_job(job)
        queue.enqueue(job.job_id)
        wakeup.set()
        return JobCreated(job_id=job.job_id, status=job.status)

    @app.post("/v1/source-assets", status_code=201, response_model=SourceAsset)
    async def upload_asset(
        request: Request,
        filename: str = Query("upload", max_length=255),
    ) -> SourceAsset:
        """Upload one image or video as the raw request body (not multipart). Use the
        returned asset_id in a job source: {"type": "image"|"video", "asset_id": ...}."""
        declared = request.headers.get("content-length")
        try:
            asset = await media.save_upload(
                request.stream(), filename, int(declared) if declared else None
            )
        except UploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        except MediaError as exc:
            raise HTTPException(422, str(exc)) from None
        repo.save_source_asset(asset.asset_id, asset.model_dump(mode="json"))
        return asset

    def _asset(asset_id: str) -> SourceAsset:
        raw = repo.get_source_asset(asset_id)
        if raw is None:
            raise HTTPException(404, "asset not found")
        return SourceAsset.model_validate(raw)

    @app.get("/v1/source-assets/{asset_id}", response_model=SourceAsset)
    async def get_asset(asset_id: str) -> SourceAsset:
        """What was extracted from an upload: size, duration, keyframes, transcript."""
        return _asset(asset_id)

    @app.get("/v1/source-assets/{asset_id}/content", response_class=FileResponse)
    async def asset_content(asset_id: str) -> FileResponse:
        """The uploaded file itself (supports range requests, so videos can seek)."""
        asset = _asset(asset_id)
        return _file(asset.file, asset.content_type)

    @app.get("/v1/source-assets/{asset_id}/frames/{index}", response_class=FileResponse)
    async def asset_frame(asset_id: str, index: int) -> FileResponse:
        """A video keyframe exactly as the model saw it."""
        asset = _asset(asset_id)
        if not 0 <= index < len(asset.frames):
            raise HTTPException(404, "frame not found")
        return _file(asset.frames[index].file, "image/jpeg")

    @app.get("/v1/drama-jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str) -> Job:
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        # The pause flag lives in the queue (a running worker would overwrite it on the job).
        job.pause_requested = job.status == "running" and queue.pause_requested(job_id)
        return job

    @app.post("/v1/drama-jobs/{job_id}/pause", status_code=202, response_model=Job)
    async def pause(job_id: str, now: bool = False) -> Job:
        """Pause a queued job now, or a running one after its current model call.
        now=true stops a running job at once, cancelling the in-flight model call (for a
        call that hangs); the interrupted step restarts on resume."""
        try:
            return pause_job(repo, queue, job_id, now=now)
        except KeyError:
            raise HTTPException(404, "job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/v1/drama-jobs/{job_id}/resume", status_code=202, response_model=Job)
    async def resume(job_id: str) -> Job:
        """Continue a paused job; steps that passed are kept."""
        try:
            job = resume_job(repo, queue, job_id)
        except KeyError:
            raise HTTPException(404, "job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        wakeup.set()
        return job

    @app.get("/v1/drama-jobs/{job_id}/trace", response_model=Trace)
    async def trace(job_id: str, after_seq: int = Query(0, ge=0)) -> Trace:
        """Agent steps and model calls. Poll with after_seq=next_seq for new events."""
        if repo.get_job(job_id) is None:
            raise HTTPException(404, "job not found")
        events = repo.list_events(job_id, after_seq)
        views = []
        for call in repo.list_llm_calls(job_id):
            tail = ""
            if call.status == "running" and (full := repo.get_llm_call(call.call_id)):
                tail = full.output[-OUTPUT_TAIL_CHARS:]
            views.append(LLMCallView(call=call, output_tail=tail))
        return Trace(
            events=events,
            calls=views,
            next_seq=events[-1].seq if events else after_seq,
        )

    @app.get("/v1/llm-calls/{call_id}", response_model=LLMCall)
    async def llm_call(call_id: str) -> LLMCall:
        """One model call in full: request messages, commentary and the complete output."""
        call = repo.get_llm_call(call_id)
        if call is None:
            raise HTTPException(404, "call not found")
        return call

    @app.post("/v1/drama-jobs/{job_id}/retry", status_code=202, response_model=Job)
    async def retry(job_id: str) -> Job:
        """Re-run a needs_revision or failed job from the step that didn't pass."""
        try:
            job = retry_job(repo, queue, job_id)
        except KeyError:
            raise HTTPException(404, "job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        wakeup.set()
        return job
