"""HTTP API (spec section 9)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..archive import archive_non_english
from ..config import (
    client_from_env,
    load_dotenv,
    queue_from_env,
    repo_from_env,
    worker_settings,
)
from ..export import build_export
from ..image_prompts import MAX_PROMPT_CHARS, ImagePromptWriter, problems
from ..imagegen import FixtureImageClient, ImageClient
from ..images import (
    SIZES,
    STALE_RUNNING_S,
    ImageWorker,
    NeedsPick,
    design_slots,
    draws_for,
    image_client_from_env,
    latest_images,
    plan_images,
    slot_title,
    take_draws,
)
from ..images import develop as develop_plan
from ..images import edit_take as edit_take_plan
from ..images import explore as explore_plan
from ..images import new_take as new_take_plan
from ..images import select as select_plan
from ..jobqueue import InMemoryJobQueue, JobQueue
from ..llm import FixtureModelClient, ModelClient
from ..media import MediaError, MediaStore, UploadTooLarge, media_from_env
from ..models import (
    CastSheet,
    Job,
    JobRequest,
    LLMCall,
    ProductionPackage,
    PropSheet,
    Revision,
    RevisionRequest,
    SetSheet,
    SourceAsset,
    TraceEvent,
)
from ..models.design_sheets import SHEET_SCHEMAS
from ..models.images import DesignOption, GeneratedImage
from ..pipeline.impact import analyze_impact
from ..pipeline.orchestrator import Orchestrator, new_id
from ..render import render_markdown
from ..store import InMemoryRepository, Repository
from ..worker import JobWorker, pause_job, recover, resume_job, retry_job

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class RevalidatingStaticFiles(StaticFiles):
    """Static files the browser must revalidate on each load (ETag makes that cheap).

    Without Cache-Control, browsers cache app.js heuristically and keep running an old
    version of the UI after an update.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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


class LLMCallView(BaseModel):
    """A model call for the live view: no request body; running calls carry an output tail."""

    call: LLMCall
    output_tail: str = ""


class Trace(BaseModel):
    events: list[TraceEvent]
    calls: list[LLMCallView]
    next_seq: int  # pass back as after_seq to get only newer events


OUTPUT_TAIL_CHARS = 4000


class Meta(BaseModel):
    model_id: str  # default model
    client: str
    stage_models: dict[str, str] = {}  # per-step overrides of the default
    image_model: str = ""
    image_client: str = ""


class ImagesView(BaseModel):
    package_id: str
    revision_id: str
    image_model: str
    images: list[GeneratedImage]
    # Over the current (newest) version of each deliverable.
    pending: int
    running: int
    done: int
    failed: int
    # Design exploration: the takes, the deliverables (anchor or drawn from one), and the
    # whole queue (every draw counts).
    options: list[DesignOption] = []
    slots: list[dict] = []
    queued: int = 0  # pending + running images of any version or draw
    notes: list[str] = []


class ExploreRequest(BaseModel):
    takes: int = Field(4, ge=1, le=6)
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    scope: list[Literal["cast", "sets", "props"]] = ["cast", "sets", "props"]
    revision_id: str | None = None
    job_id: str | None = None


class DevelopRequest(BaseModel):
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    revision_id: str | None = None
    job_id: str | None = None


class NewTakeRequest(BaseModel):
    requirement_id: str
    deliverable_id: str
    direction: str | None = Field(None, max_length=500)
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    revision_id: str | None = None
    job_id: str | None = None


class DrawsRequest(BaseModel):
    draws: int = Field(3, ge=1, le=6)


class EditTakeRequest(BaseModel):
    prompt: str = Field(..., max_length=MAX_PROMPT_CHARS)
    draws: int | None = Field(None, ge=1, le=6)  # default: as many as the source take has


EXPORT_DIR = Path(tempfile.gettempdir()) / "drama-exports"
EXPORT_MAX_AGE_S = 3600


def _new_export_file() -> Path:
    """A fresh temp file for an export zip. Zips are deleted after they are sent; one left
    behind by a cancelled download (the cleanup task doesn't run then) is swept here once
    it is an hour old."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - EXPORT_MAX_AGE_S
    for old in EXPORT_DIR.glob("drama-export-*.zip"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    fd, path = tempfile.mkstemp(prefix="drama-export-", suffix=".zip", dir=EXPORT_DIR)
    os.close(fd)  # build_export reopens it by path
    return Path(path)


def _seed_label(job: Job, assets: dict[str, dict]) -> str:
    """Job list label: the first text, else the first uploaded file's name."""
    sources = job.request.sources
    text = next((s.text for s in sources if s.type in ("text", "script") and s.text), None)
    if text:
        return text
    prompt = next((s.text for s in sources if s.text), None)
    first = sources[0]
    name = (assets.get(first.asset_id or "") or {}).get("filename") or first.asset_id or ""
    label = f"[{first.type}] {name}"
    return f"{label} · {prompt}" if prompt else label


def create_app(
    repo: Repository | None = None,
    client: ModelClient | None = None,
    queue: JobQueue | None = None,
    workers: int = 1,
    lease_s: float = 30.0,
    poll_s: float = 1.0,
    retry_base_s: float = 5.0,
    media: MediaStore | None = None,
    image_client: ImageClient | None = None,
) -> FastAPI:
    """Build the API. `workers` in-process job workers start with the app (lifespan);
    with workers=0 jobs are only enqueued, for a separate `python -m drama_agent.worker`.
    Uploads go to `media` (default: a fresh temporary directory)."""
    repo = repo or InMemoryRepository()
    queue = queue or InMemoryJobQueue()
    media = media or MediaStore(tempfile.mkdtemp(prefix="drama-media-"))
    orchestrator = Orchestrator(
        repo, client or FixtureModelClient(), checkpoints=queue, media=media
    )
    images = image_client or FixtureImageClient()
    wakeup = asyncio.Event()
    image_wakeup = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Stories from before the app became English-only are archived, not deleted.
        archive_non_english(repo)
        n = recover(repo, queue)
        if n:
            log.info("enqueued %d unfinished jobs without a queue entry", n)
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(
                JobWorker(
                    repo,
                    queue,
                    orchestrator,
                    lease_s=lease_s,
                    poll_s=poll_s,
                    retry_base_s=retry_base_s,
                ).run(stop, wakeup)
            )
            for _ in range(workers)
        ]
        # One image worker per process: the GPU server makes one image at a time anyway.
        # Images another process is still generating are younger than the stale limit.
        if n := repo.requeue_running_images(STALE_RUNNING_S):
            log.info("requeued %d interrupted images", n)
        writer = ImagePromptWriter(orchestrator.client, repo)
        tasks.append(
            asyncio.create_task(
                ImageWorker(repo, media, images, writer=writer).run(stop, image_wakeup)
            )
        )
        try:
            yield
        finally:
            stop.set()
            # Cancelling releases in-flight jobs back to the queue (no attempt spent).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if close := getattr(images, "aclose", None):
                await close()

    app = FastAPI(title="Drama Agent", version="0.1.0", lifespan=lifespan)
    app.state.repo = repo
    app.state.queue = queue
    app.state.orchestrator = orchestrator
    app.state.media = media

    @app.get("/v1/meta", response_model=Meta)
    async def meta() -> Meta:
        c = orchestrator.client
        return Meta(
            model_id=c.model_id,
            client=type(c).__name__,
            stage_models=getattr(c, "stage_models", {}),
            image_model=images.model_id,
            image_client=type(images).__name__,
        )

    # Reference images -------------------------------------------------------

    def _package_or_404(package_id: str, revision_id: str | None) -> ProductionPackage:
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        return pkg

    def _images_view(pkg: ProductionPackage, notes: list[str] | None = None) -> ImagesView:
        imgs = repo.list_images(pkg.package_id, pkg.revision_id)
        latest = latest_images(imgs)
        counts = {s: 0 for s in ("pending", "running", "done", "failed")}
        for img in latest.values():
            counts[img.status] += 1
        return ImagesView(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            image_model=images.model_id,
            images=imgs,
            options=repo.list_options(pkg.package_id, pkg.revision_id),
            slots=design_slots(pkg),
            queued=sum(1 for i in imgs if i.status in ("pending", "running")),
            notes=notes or [],
            **counts,
        )

    @app.get("/v1/drama-packages/{package_id}/images", response_model=ImagesView)
    async def list_package_images(package_id: str, revision_id: str | None = None) -> ImagesView:
        """Reference images of a package revision, every version (newest per deliverable
        is the current one), with counts over the current ones."""
        return _images_view(_package_or_404(package_id, revision_id))

    @app.post("/v1/drama-packages/{package_id}/images", status_code=202, response_model=ImagesView)
    async def generate_package_images(
        package_id: str,
        revision_id: str | None = None,
        draft: bool = False,
        job_id: str | None = None,
    ) -> ImagesView:
        """Queue images for every character, look, location and prop deliverable that has
        none yet (or whose last attempt failed). draft=true makes small, fast images.
        job_id (the job that made the package) is where the prompt writer's call is traced;
        the web UI sends it."""
        pkg = _package_or_404(package_id, revision_id)
        job = _package_job(pkg.package_id, job_id)
        existing = repo.list_images(pkg.package_id, pkg.revision_id)
        for img in plan_images(pkg, existing, draft=draft, job_id=job and job.job_id):
            repo.save_image(img)
        image_wakeup.set()
        return _images_view(pkg)

    def _package_job(
        package_id: str, job_id: str | None, revision_id: str | None = None
    ) -> Job | None:
        """The job that made a package; refuses archived stories."""
        if job_id is not None:
            job = repo.get_job(job_id)
            if job is None or job.package_id != package_id:
                raise HTTPException(422, f"job {job_id} did not make package {package_id}")
        else:
            # Prefer the job that made this exact revision, else the newest for the package.
            jobs = repo.jobs_for_package(package_id)
            job = next((j for j in jobs if j.revision_id == revision_id), None) or next(
                iter(jobs), None
            )
        if job is not None and job.archived:
            raise HTTPException(409, "this story is archived")
        return job

    def _save_new(opts: list[DesignOption], imgs: list[GeneratedImage]) -> None:
        for o in opts:
            repo.save_option(o)
        for i in imgs:
            repo.save_image(i)
        image_wakeup.set()

    @app.post(
        "/v1/drama-packages/{package_id}/images/explore",
        status_code=202,
        response_model=ImagesView,
    )
    async def explore_designs(package_id: str, req: ExploreRequest) -> ImagesView:
        """Phase 1: `takes` distinct designs x `draws` images for every anchor (each
        character's face, each location's and prop's first state) in scope. Pick one image
        per anchor with /select, then /develop the rest from your picks."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        opts, imgs = explore_plan(
            pkg,
            repo.list_options(pkg.package_id, pkg.revision_id),
            takes=req.takes,
            draws=req.draws,
            draft=req.draft,
            scope=list(req.scope),
            job_id=job and job.job_id,
        )
        _save_new(opts, imgs)
        return _images_view(pkg)

    @app.post(
        "/v1/drama-packages/{package_id}/images/develop",
        status_code=202,
        response_model=ImagesView,
    )
    async def develop_designs(package_id: str, req: DevelopRequest) -> ImagesView:
        """Phase 2: looks, other states and plan views, drawn from the selected anchor
        images and their takes, `draws` images each. Deliverables whose anchor has no
        selection yet are left for later (listed in `notes`)."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        opts, imgs, waiting = develop_plan(
            pkg,
            repo.list_options(pkg.package_id, pkg.revision_id),
            repo.list_images(pkg.package_id, pkg.revision_id),
            draws=req.draws,
            draft=req.draft,
            job_id=job and job.job_id,
        )
        _save_new(opts, imgs)
        notes = [f"Pick an image for {slot_title(pkg, k)} first." for k in waiting]
        return _images_view(pkg, notes)

    @app.post(
        "/v1/drama-packages/{package_id}/images/takes",
        status_code=202,
        response_model=list[DesignOption],
    )
    async def add_take(package_id: str, req: NewTakeRequest) -> list[DesignOption]:
        """One more take on a deliverable, optionally steered (`direction`)."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        try:
            opts, imgs = new_take_plan(
                pkg,
                repo.list_options(pkg.package_id, pkg.revision_id),
                repo.list_images(pkg.package_id, pkg.revision_id),
                (req.requirement_id, req.deliverable_id),
                direction=(req.direction or "").strip() or None,
                draws=req.draws,
                draft=req.draft,
                job_id=job and job.job_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except NeedsPick as exc:
            raise HTTPException(
                409, f"Pick an image for {slot_title(pkg, exc.key)} first."
            ) from None
        _save_new(opts, imgs)
        return opts

    @app.post(
        "/v1/image-options/{option_id}/draws",
        status_code=202,
        response_model=list[GeneratedImage],
    )
    async def more_draws(option_id: str, req: DrawsRequest) -> list[GeneratedImage]:
        """More images of an existing take (new seeds, same prompt)."""
        opt = repo.get_option(option_id)
        if opt is None:
            raise HTTPException(404, "take not found")
        pkg = _package_or_404(opt.package_id, opt.revision_id)
        _package_job(pkg.package_id, opt.job_id, pkg.revision_id)
        mine = take_draws(repo.list_images(pkg.package_id, pkg.revision_id), option_id)
        refs = mine[0].reference_ids if mine else []
        first = 1 + max((i.draw for i in mine), default=0)
        imgs = draws_for(opt, req.draws, refs, first=first)
        _save_new([], imgs)
        return imgs

    @app.post(
        "/v1/image-options/{option_id}/edit",
        status_code=202,
        response_model=DesignOption,
    )
    async def edit_take(option_id: str, req: EditTakeRequest) -> DesignOption:
        """A new take with your own wording of this take's prompt, drawn with the same seeds
        (and references) as this take's draws, so the images show what the wording changed.
        A prompt that would break the render (cites a picture the take doesn't have) is
        refused; other H3 rule misses are kept as `warnings` on the new take."""
        source = repo.get_option(option_id)
        if source is None:
            raise HTTPException(404, "take not found")
        if source.status == "pending":
            raise HTTPException(409, "this take is still being written")
        pkg = _package_or_404(source.package_id, source.revision_id)
        _package_job(pkg.package_id, source.job_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        mine = take_draws(images, option_id)
        prompt = req.prompt.strip()
        n_refs = len(mine[0].reference_ids) if mine else 0
        found = problems(prompt, n_refs)
        fatal = [p for p in found if p == "empty" or p.startswith("cites a picture")]
        if fatal:
            raise HTTPException(422, f"prompt {'; '.join(fatal)}")
        opt, imgs = edit_take_plan(
            source,
            repo.list_options(pkg.package_id, pkg.revision_id),
            images,
            prompt,
            draws=req.draws or max(1, min(6, len(mine))),
            warnings=found,
        )
        _save_new([opt], imgs)
        return opt

    @app.post(
        "/v1/generated-images/{image_id}/select",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def select_image(image_id: str) -> GeneratedImage:
        """Choose this image for its deliverable. A draft is re-rendered at full size; the
        full-size image takes over the selection when it's done (picking a draft whose
        full-size render is already done picks the render, which is returned)."""
        img = _image_or_404(image_id)
        pkg = _package_or_404(img.package_id, img.revision_id)
        _package_job(pkg.package_id, img.job_id, pkg.revision_id)
        try:
            pick, final = select_plan(repo.list_images(pkg.package_id, pkg.revision_id), image_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        repo.set_selected(pick)
        if final is not None:
            _save_new([], [final])
        return repo.get_image(pick)

    def _image_or_404(image_id: str) -> GeneratedImage:
        img = repo.get_image(image_id)
        if img is None:
            raise HTTPException(404, "image not found")
        return img

    @app.post(
        "/v1/generated-images/{image_id}/regenerate",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def regenerate_image(image_id: str, draft: bool | None = None) -> GeneratedImage:
        """A new version of this image's deliverable, with a fresh seed. References use the
        current versions of the images it depends on."""
        old = _image_or_404(image_id)
        pkg = _package_or_404(old.package_id, old.revision_id)
        _package_job(pkg.package_id, old.job_id)
        small = draft if draft is not None else old.size in {s[1] for s in SIZES.values()}
        [new] = plan_images(
            pkg,
            repo.list_images(pkg.package_id, pkg.revision_id),
            draft=small,
            only=(old.requirement_id, old.deliverable_id),
            job_id=old.job_id,
        )
        repo.save_image(new)
        image_wakeup.set()
        return new

    @app.post(
        "/v1/generated-images/{image_id}/retry",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def retry_image(image_id: str) -> GeneratedImage:
        """Queue a failed image again as it was: same prompt, seed, size and references (a
        failed draw of a take stays that draw)."""
        img = _image_or_404(image_id)
        pkg = _package_or_404(img.package_id, img.revision_id)
        _package_job(pkg.package_id, img.job_id, pkg.revision_id)
        if img.status != "failed":
            raise HTTPException(
                409, f"only a failed image can be retried (this one is {img.status})"
            )
        img.status, img.error, img.started_at, img.finished_at = "pending", None, None, None
        repo.save_image(img)
        image_wakeup.set()
        return img

    @app.get("/v1/generated-images/{image_id}", response_model=GeneratedImage)
    async def get_image(image_id: str) -> GeneratedImage:
        return _image_or_404(image_id)

    @app.get("/v1/generated-images/{image_id}/{variant}", response_class=FileResponse)
    async def image_file(image_id: str, variant: Literal["content", "thumb"]) -> FileResponse:
        img = _image_or_404(image_id)
        rel = img.thumb if variant == "thumb" and img.thumb else img.file
        if rel is None:
            raise HTTPException(404, f"image is {img.status}")
        return _file(rel, "image/jpeg" if rel.endswith(".jpg") else img.content_type)

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

    def _file(rel: str, content_type: str) -> FileResponse:
        try:
            path = media.path(rel)
        except MediaError:
            raise HTTPException(404, "file not found") from None
        if not path.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(
            path,
            media_type=content_type,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

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
            # text/plain, not text/markdown: browsers display it (and its text can be
            # copied) instead of downloading it.
            return PlainTextResponse(render_markdown(pkg), media_type="text/plain")
        return pkg

    @app.get("/v1/drama-packages/{package_id}/export", response_class=FileResponse)
    async def export_package(
        package_id: str, revision_id: str | None = None, job_id: str | None = None
    ) -> FileResponse:
        """Everything a production team needs, as one zip: story bible, scripts, storyboard
        and shot list (CSV), cast/set/prop sheets, asset requirements with the generated
        reference images, QA report, sources and the full package JSON."""
        pkg = _package_or_404(package_id, revision_id)
        job = _package_job(pkg.package_id, job_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        ids = [s.asset_id for s in job.request.sources if s.asset_id] if job else []
        assets = {k: SourceAsset.model_validate(v) for k, v in repo.get_source_assets(ids).items()}
        tmp = _new_export_file()
        try:
            name = await asyncio.to_thread(
                build_export,
                pkg,
                tmp,
                job=job,
                images=images,
                assets=assets,
                media=media,
                options=repo.list_options(pkg.package_id, pkg.revision_id),
            )
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=name,
            background=BackgroundTask(tmp.unlink, missing_ok=True),
        )

    @app.get("/v1/drama-packages/{package_id}/{sheet}", response_model=None)
    async def get_sheet(
        package_id: str,
        sheet: Literal["cast-sheet", "set-sheet", "prop-sheet"],
        revision_id: str | None = None,
    ) -> CastSheet | SetSheet | PropSheet:
        """One design sheet as its own document (schema: /v1/schemas/{sheet}.json)."""
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        doc = getattr(pkg, sheet.replace("-", "_"))
        if doc is None:
            raise HTTPException(404, f"this package was made before {sheet}s existed")
        return doc

    @app.get("/v1/schemas/{name}.json")
    async def get_schema(name: str) -> dict:
        """JSON Schema of a standalone document: cast-sheet, set-sheet or prop-sheet."""
        model = SHEET_SCHEMAS.get(name)
        if model is None:
            raise HTTPException(404, f"unknown schema {name}; known: {', '.join(SHEET_SCHEMAS)}")
        schema = model.model_json_schema()
        schema["$id"] = f"/v1/schemas/{name}.json"
        return schema

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
        app.mount("/", RevalidatingStaticFiles(directory=WEB_DIR, html=True), name="web")

    return app


def create_app_from_env() -> FastAPI:
    """Entry point for uvicorn: reads .env and picks the model client from DRAMA_MODEL_CLIENT."""
    load_dotenv()
    repo = repo_from_env()
    settings = worker_settings()
    return create_app(
        repo=repo,
        client=client_from_env(),
        queue=queue_from_env(repo),
        workers=int(settings["workers"]),
        lease_s=float(settings["lease_s"]),
        media=media_from_env(),
        image_client=image_client_from_env(),
    )


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
