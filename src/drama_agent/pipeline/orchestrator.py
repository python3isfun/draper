"""Single orchestrator running five gated steps with bounded retries (spec sections 4, 8)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Protocol

from ..llm import GenerationRequest, ModelClient
from ..models import Job, ProductionPackage, Revision
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
    ) -> None:
        self.repo = repo
        self.client = client
        self.checkpoints = checkpoints
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
                    gen = GenerationRequest(
                        stage=step.name,
                        instructions=(f"{self.base_prompt}\n\n{step.directive}\n\n{OUTPUT_RULES}"),
                        context=state.context(),
                        feedback=issues,
                        attempt=attempt,
                        recorder=trace.recorder(step.name, attempt),
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
