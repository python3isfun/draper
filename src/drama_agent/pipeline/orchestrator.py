"""Single orchestrator running five gated steps with bounded retries (spec sections 4, 8)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Protocol

from ..llm import GenerationRequest, ModelClient
from ..llm.base import Transcriber
from ..media import MediaPart, MediaStore, source_media
from ..models import Job, ProductionPackage, Revision, SourceAsset, TranscriptSegment
from ..models.package import utcnow
from ..prompts import load_orchestrator_prompt
from ..store import Repository
from ..tracing import Tracer
from .steps import OUTPUT_RULES, STEPS, PipelineState

log = logging.getLogger(__name__)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CheckpointStore(Protocol):
    """The job queue: per-job checkpoints, plus (optionally) `pause_requested(job_id)`."""

    def save_checkpoint(self, job_id: str, data: str) -> None: ...
    def load_checkpoint(self, job_id: str) -> str | None: ...


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        client: ModelClient,
        checkpoints: CheckpointStore | None = None,
        media: MediaStore | None = None,
    ) -> None:
        self.repo = repo
        self.client = client
        self.checkpoints = checkpoints
        self.media = media
        self._asset_locks: dict[str, asyncio.Lock] = {}
        self.base_prompt = load_orchestrator_prompt()

    async def run_job(self, job_id: str) -> Job:
        """Run (or resume) a job. Returns it in a terminal or paused state, or raises
        CancelledError if the surrounding task is cancelled (e.g. lost lease)."""
        job = self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status == "cancelled":
            return job

        trace = Tracer(self.repo, job_id)
        state, completed = self._restore(job)
        state.emit = lambda kind, msg, **kw: trace.event(kind, msg, **kw)
        budget = job.request.options.revision_budget_per_stage
        self._set(job, status="running", failure_reason=None)
        if completed:
            trace.event(
                "job_resumed", f"Resumed; keeping {', '.join(completed)} from the checkpoint."
            )
        else:
            trace.event("job_started", "Started.")
        stage = None

        # Everything, including the final save, is inside the try so the job can never be
        # left in 'running' by an unexpected exception.
        try:
            for step in STEPS:
                stage = job.stages[step.name]
                if step.name in completed:
                    stage.status = "passed"  # restored from checkpoint; not re-run
                    continue
                # Safe point between steps: pause before announcing the next one.
                if self._pause_requested(job_id):
                    return self._pause(job, stage, trace, step.name)
                stage.status, stage.error, stage.issues = "running", None, []
                self._touch(job)
                trace.event(
                    "stage_started", f"{step.name.capitalize()} step started.", stage=step.name
                )
                started = time.monotonic()
                issues = []
                media_ctx, media_parts = [], []
                if step.name == "ingest":
                    media_ctx, media_parts = await self._prepare_media(job, state, trace)
                # One initial attempt plus `budget` automatic repairs. Only this step is
                # retried; earlier passed steps are not re-run.
                for attempt in range(budget + 1):
                    # Safe point: nothing is in flight, so pausing here loses no work
                    # except this step's unfinished attempts.
                    if self._pause_requested(job_id):
                        return self._pause(job, stage, trace, step.name)
                    stage.attempts = attempt + 1
                    if attempt:
                        trace.event(
                            "attempt_started",
                            f"Attempt {attempt + 1} of {budget + 1}, with {len(issues)} "
                            "issues as feedback.",
                            stage=step.name,
                            attempt=attempt,
                        )
                    context = state.context()
                    if media_ctx:
                        context["source_media"] = media_ctx
                    gen = GenerationRequest(
                        stage=step.name,
                        instructions=(f"{self.base_prompt}\n\n{step.directive}\n\n{OUTPUT_RULES}"),
                        context=context,
                        feedback=issues,
                        attempt=attempt,
                        recorder=trace.recorder(step.name, attempt),
                        media=media_parts,
                    )
                    try:
                        output = await self.client.generate(gen, step.output_model)
                    finally:
                        # Count tokens even when the call ultimately fails.
                        used = state.token_usage.setdefault(step.name, {})
                        for k, v in gen.usage.items():
                            used[k] = used.get(k, 0) + v
                    state.model_versions[step.name] = gen.model or self.client.model_id
                    step.apply(state, output)
                    issues = step.check(state)
                    if not issues:
                        break
                    trace.event(
                        "gate_issues",
                        f"Hard checks found {len(issues)} issues.",
                        stage=step.name,
                        attempt=attempt,
                        issues=[f"{i.check} {i.object_id}: {i.evidence}" for i in issues[:10]],
                    )
                stage.issues = issues
                if issues:
                    stage.status = "failed"
                    # Keep the draft (with its issues) so the user can inspect and revise it.
                    state.qa_report.hard_issues = issues
                    trace.event(
                        "stage_failed",
                        f"{step.name.capitalize()} still had {len(issues)} issues after "
                        f"{budget + 1} attempts; stopping as needs_revision.",
                        stage=step.name,
                    )
                    self._finish(job, state, status="needs_revision")
                    return job
                # Checkpoint before marking passed: a crash in between re-runs this step
                # rather than skipping it with missing output.
                completed.append(step.name)
                if self.checkpoints:
                    self.checkpoints.save_checkpoint(job_id, state.to_checkpoint(completed))
                stage.status = "passed"
                self._touch(job)
                used = state.token_usage.get(step.name, {})
                trace.event(
                    "stage_passed",
                    f"{step.name.capitalize()} passed in {time.monotonic() - started:.0f}s "
                    f"({stage.attempts} attempt{'s' if stage.attempts > 1 else ''}).",
                    stage=step.name,
                    tokens=used,
                )
            stage = None
            self._finish(job, state, status="completed")
            trace.event("job_completed", "Completed; all hard checks passed.")
        except Exception as exc:  # job must record any failure
            log.exception("job %s failed", job_id)
            reason = f"{type(exc).__name__}: {exc}"
            if stage is not None:
                stage.status = "failed"
                stage.error = reason
            self._set(job, status="failed", failure_reason=reason)
            trace.event(
                "job_failed", f"Failed: {reason}", stage=stage and self._stage_name(job, stage)
            )
        return job

    async def _prepare_media(
        self, job: Job, state: PipelineState, trace: Tracer
    ) -> tuple[list[dict], list[MediaPart]]:
        """Load the job's uploaded media, transcribe videos (cached on the asset) and return
        the ingest step's context entries and images."""
        assets: dict[str, SourceAsset] = {}
        for src in job.request.sources:
            if src.type in ("image", "video") and src.asset_id:
                raw = self.repo.get_source_asset(src.asset_id)
                if raw is None:
                    raise RuntimeError(f"source asset {src.asset_id} not found")
                assets[src.asset_id] = SourceAsset.model_validate(raw)
        state.source_assets = assets
        if not assets:
            return [], []
        if self.media is None:
            raise RuntimeError("this job has image/video sources but no media store is set up")
        for asset_id, asset in assets.items():
            if asset.kind == "video" and asset.transcript_status in ("pending", "failed"):
                assets[asset_id] = await self._transcribe(asset, trace)
        ctx, parts = source_media(job.request, assets, self.media)
        trace.event(
            "media_attached",
            f"Showing the model {len(parts)} image{'s' if len(parts) != 1 else ''} from "
            f"{len(assets)} uploaded file{'s' if len(assets) != 1 else ''}.",
            stage="ingest",
            images=[p.label for p in parts],
        )
        return ctx, parts

    async def _transcribe(self, asset: SourceAsset, trace: Tracer) -> SourceAsset:
        """Transcribe a video's audio once; returns the asset as now stored."""
        if not isinstance(self.client, Transcriber):
            return asset  # offline client: the audio stays "not analyzed"
        assert self.media is not None
        # Jobs sharing an upload wait for each other instead of transcribing (and paying)
        # twice. Separate worker processes still could; their audio files don't collide.
        async with self._asset_locks.setdefault(asset.asset_id, asyncio.Lock()):
            raw = self.repo.get_source_asset(asset.asset_id)
            if raw is not None:
                asset = SourceAsset.model_validate(raw)
            if asset.transcript_status not in ("pending", "failed"):
                return asset  # done by another job while this one waited
            await self._run_transcription(asset, trace, self.client)
            self.repo.save_source_asset(asset.asset_id, asset.model_dump(mode="json"))
            return asset

    async def _run_transcription(
        self, asset: SourceAsset, trace: Tracer, client: Transcriber
    ) -> None:
        assert self.media is not None
        model = client.transcribe_model
        rec = trace.recorder("ingest", 0)
        rec.start(model=model, api="transcription", request={"asset_id": asset.asset_id})
        rec.delta("commentary", f"Transcribing the audio of {asset.filename}.")
        audio = None
        try:
            audio, cut_ms = await asyncio.to_thread(self.media.extract_audio, asset)
            segments = await client.transcribe(audio)
        except Exception as exc:  # the job goes on; the audio is recorded as unknown
            log.warning("asset %s: transcription failed", asset.asset_id, exc_info=True)
            asset.transcript_status = "failed"
            asset.transcript_error = f"{type(exc).__name__}: {exc}"[:300]
            rec.finish(error=asset.transcript_error)
            return
        finally:
            if audio is not None:
                audio.unlink(missing_ok=True)
        # Whisper rounds segment ends up; keep every timestamp inside the video.
        end = asset.duration_ms or None
        asset.transcript = [
            TranscriptSegment(
                start_ms=min(seg["start_ms"], end) if end else seg["start_ms"],
                end_ms=min(seg["end_ms"], end) if end else seg["end_ms"],
                text=seg["text"],
            )
            for seg in segments
        ]
        asset.transcript_status = "done"
        asset.transcript_until_ms = cut_ms
        asset.transcript_model = model
        asset.transcript_error = None
        rec.delta(
            "output",
            "\n".join(f"[{s.start_ms}-{s.end_ms} ms] {s.text}" for s in asset.transcript)
            or "(no speech detected)",
        )
        rec.finish()

    def _pause_requested(self, job_id: str) -> bool:
        check = getattr(self.checkpoints, "pause_requested", None)
        return bool(check and check(job_id))

    def _pause(self, job: Job, stage, trace: Tracer, stage_name: str) -> Job:
        # The interrupted step restarts from its first attempt on resume; passed steps are
        # kept by the checkpoint.
        stage.status, stage.attempts, stage.issues, stage.error = "pending", 0, [], None
        self._set(job, status="paused", failure_reason=None)
        trace.event(
            "paused", f"Paused before the {stage_name} step's next model call.", stage=stage_name
        )
        return job

    @staticmethod
    def _stage_name(job: Job, stage) -> str | None:
        return next((name for name, s in job.stages.items() if s is stage), None)

    # ------------------------------------------------------------------

    def _restore(self, job: Job) -> tuple[PipelineState, list[str]]:
        raw = self.checkpoints.load_checkpoint(job.job_id) if self.checkpoints else None
        if raw:
            state, completed = PipelineState.from_checkpoint(job.request, raw)
            log.info("job %s resuming after %s", job.job_id, completed)
            return state, completed
        fresh = PipelineState(
            request=job.request, package_id=new_id("pkg"), revision_id=new_id("rev")
        )
        return fresh, []

    def _finish(self, job: Job, state: PipelineState, status: str) -> None:
        pkg: ProductionPackage | None = None
        if state.source_brief and state.story_bible:
            pkg = state.draft_package()
            # Idempotent: a resumed job may already have saved this revision before a crash.
            if self.repo.get_package(pkg.package_id, pkg.revision_id) is None:
                self.repo.save_package(pkg, published=status == "completed")
            self.repo.save_revision(
                Revision(
                    revision_id=pkg.revision_id,
                    package_id=pkg.package_id,
                    base_revision_id=None,
                    status="published" if status == "completed" else "needs_revision",
                )
            )
            self.repo.save_evaluation_report(pkg.package_id, pkg.revision_id, state.qa_report)
        self._set(
            job,
            status=status,
            package_id=pkg.package_id if pkg else None,
            revision_id=pkg.revision_id if pkg else None,
        )

    def _set(self, job: Job, **fields: object) -> None:
        for k, v in fields.items():
            setattr(job, k, v)
        self._touch(job)

    def _touch(self, job: Job) -> None:
        job.updated_at = utcnow()
        self.repo.save_job(job)
