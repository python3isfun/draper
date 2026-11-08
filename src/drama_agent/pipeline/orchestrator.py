"""Single orchestrator running five gated steps with bounded retries (spec sections 4, 8)."""

from __future__ import annotations

import logging
import uuid

from ..llm import GenerationRequest, ModelClient
from ..models import Job, ProductionPackage, Revision
from ..models.package import utcnow
from ..prompts import load_orchestrator_prompt
from ..store import Repository
from .steps import STEPS, PipelineState

log = logging.getLogger(__name__)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Orchestrator:
    def __init__(self, repo: Repository, client: ModelClient) -> None:
        self.repo = repo
        self.client = client
        self.base_prompt = load_orchestrator_prompt()

    async def run_job(self, job_id: str) -> Job:
        job = self.repo.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status == "cancelled":
            return job

        state = PipelineState(
            request=job.request, package_id=new_id("pkg"), revision_id=new_id("rev")
        )
        budget = job.request.options.revision_budget_per_stage
        self._set(job, status="running")
        stage = None

        # Everything, including the final save, is inside the try so the job can never be
        # left in 'running' by an unexpected exception.
        try:
            for step in STEPS:
                stage = job.stages[step.name]
                stage.status = "running"
                self._touch(job)
                issues = []
                # One initial attempt plus `budget` automatic repairs. Only this step is
                # retried; earlier passed steps are not re-run.
                for attempt in range(budget + 1):
                    stage.attempts = attempt + 1
                    output = await self.client.generate(
                        GenerationRequest(
                            stage=step.name,
                            instructions=f"{self.base_prompt}\n\n{step.directive}",
                            context=state.context(),
                            feedback=issues,
                            attempt=attempt,
                        ),
                        step.output_model,
                    )
                    state.model_versions[step.name] = self.client.model_id
                    step.apply(state, output)
                    issues = step.check(state)
                    if not issues:
                        break
                stage.issues = issues
                if issues:
                    stage.status = "failed"
                    # Keep the draft (with its issues) so the user can inspect and revise it.
                    state.qa_report.hard_issues = issues
                    self._finish(job, state, status="needs_revision")
                    return job
                stage.status = "passed"
                self._touch(job)
            stage = None
            self._finish(job, state, status="completed")
        except Exception as exc:  # job must record any failure
            log.exception("job %s failed", job_id)
            reason = f"{type(exc).__name__}: {exc}"
            if stage is not None:
                stage.status = "failed"
                stage.error = reason
            self._set(job, status="failed", failure_reason=reason)
        return job

    # ------------------------------------------------------------------

    def _finish(self, job: Job, state: PipelineState, status: str) -> None:
        pkg: ProductionPackage | None = None
        if state.source_brief and state.story_bible:
            pkg = state.draft_package()
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
