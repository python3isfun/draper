"""HTTP API (spec section 9)."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..archive import archive_non_english
from ..config import (
    client_from_env,
    load_dotenv,
    queue_from_env,
    repo_from_env,
    worker_settings,
)
from ..experiment import (
    ExperimentRunner,
    ExpSession,
    ExpTurn,
    apply_labels,
    is_busy,
    unlabeled,
)
from ..image_prompts import ImagePromptWriter
from ..imagegen import FixtureImageClient, ImageClient
from ..images import (
    STALE_RUNNING_S,
    ImageWorker,
    image_client_from_env,
    keyframe_providers_from_env,
)
from ..jobqueue import InMemoryJobQueue, JobQueue
from ..library import sync_library
from ..llm import FixtureModelClient, ModelClient
from ..media import MediaError, MediaStore, media_from_env
from ..models import (
    Job,
    ProductionPackage,
)
from ..pipeline.orchestrator import Orchestrator
from ..pipeline.revise import ReviseRunner
from ..shot_frames import ShotFrameRunner
from ..store import InMemoryRepository, Repository
from ..worker import JobWorker, recover
from . import designs as designs_routes
from . import experiments as experiments_routes
from . import jobs as jobs_routes
from . import library as library_api
from . import packages as packages_routes
from . import shot_frames as keyframe_api
from .schemas import (
    WEB_DIR,
    Meta,
    RevalidatingStaticFiles,
)

log = logging.getLogger(__name__)


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
    keyframe_providers: dict[str, ImageClient] | None = None,
    keyframe_provider: str = "h3",
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
    experiments = ExperimentRunner(repo, media, images, orchestrator.client)

    # Keyframes share the Experiment tab's GPU lock: one render at a time from this process.
    # Keyframes can also be drawn with other providers (e.g. Nano Banana), chosen per take.
    keyframes = ShotFrameRunner(repo, media, images, orchestrator.client, gpu=experiments._gpu,
                                providers=keyframe_providers, default_provider=keyframe_provider)  # fmt: skip
    # Targeted revisions of a published story; the library mirrors the new revision.
    revisions = ReviseRunner(repo, orchestrator, after_publish=lambda: sync_library(repo))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Stories from before the app became English-only are archived, not deleted.
        archive_non_english(repo)
        try:  # the asset library mirrors every story (library/); never blocks startup
            made = sync_library(repo, force=True)
            log.info("library: %d stories, %d assets added, %d updated, %d pictures",
                     made.stories, made.added, made.updated, made.pictures)  # fmt: skip
        except Exception:
            log.warning("library sync failed", exc_info=True)
        if n := experiments.recover():
            log.info("marked %d interrupted experiment turns failed", n)
        if n := keyframes.recover():
            log.info("marked %d interrupted keyframe takes failed", n)
        if n := revisions.recover():
            log.info("marked %d interrupted revisions failed", n)
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
            await experiments.aclose()
            await keyframes.aclose()
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

    def _package_or_404(package_id: str, revision_id: str | None) -> ProductionPackage:
        pkg = repo.get_package(package_id, revision_id)
        if pkg is None:
            raise HTTPException(404, "package not found")
        return pkg

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

    def _exp_session_or_404(session_id: str) -> ExpSession:
        session = repo.get_exp_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    def _exp_turns(session_id: str) -> list[ExpTurn]:
        """A chat's turns. Images made before numbering existed get numbers once, oldest
        first (not while a message runs: the runner is saving that turn)."""
        turns = repo.list_exp_turns(session_id)
        if not is_busy(turns) and (n := unlabeled(turns)):
            for t in apply_labels(turns, repo.allocate_exp_labels(session_id, n)):
                repo.save_exp_turn(t)
        return turns

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

    # What the route modules share (each takes the names it uses).

    ctx = SimpleNamespace(**locals())

    designs_routes.register(app, ctx)

    experiments_routes.register(app, ctx)

    jobs_routes.register(app, ctx)

    library_api.register(app, repo=repo, media=media)

    keyframe_api.register(
        app,
        repo=repo,
        media=media,
        runner=keyframes,
        text_client=orchestrator.client,
        image_model=images.model_id,
        package_or_404=_package_or_404,
        package_job=_package_job,
        file_response=_file,
    )

    # Last: its /v1/drama-packages/{package_id}/{sheet} route matches what the routes
    # above don't (the keyframe and library routes under the same prefix come first).
    packages_routes.register(app, ctx)

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
        keyframe_providers=keyframe_providers_from_env(),
        keyframe_provider=os.environ.get("DRAMA_KEYFRAME_PROVIDER", "h3"),
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
