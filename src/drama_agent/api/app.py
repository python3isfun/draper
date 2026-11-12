"""HTTP API (spec section 9)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..archive import archive_non_english
from ..config import (
    client_from_env,
    load_dotenv,
    queue_from_env,
    repo_from_env,
    worker_settings,
)
from ..image_prompts import ImagePromptWriter
from ..imagegen import FixtureImageClient, ImageClient
from ..images import (
    SIZES,
    STALE_RUNNING_S,
    ImageWorker,
    image_client_from_env,
    latest_images,
    plan_images,
)
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
from ..models.images import GeneratedImage
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

    def _images_view(pkg: ProductionPackage) -> ImagesView:
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

    def _package_job(package_id: str, job_id: str | None) -> Job | None:
        """The job that made a package; refuses archived stories."""
        if job_id is not None:
            job = repo.get_job(job_id)
            if job is None or job.package_id != package_id:
                raise HTTPException(422, f"job {job_id} did not make package {package_id}")
        else:
            job = next((j for j in repo.list_jobs(1000) if j.package_id == package_id), None)
        if job is not None and job.archived:
            raise HTTPException(409, "this story is archived")
        return job

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
    async def pause(job_id: str) -> Job:
        """Pause a queued job now, or a running one after its current model call."""
        try:
            return pause_job(repo, queue, job_id)
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
