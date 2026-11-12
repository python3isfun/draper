"""Storage for the five V1 object kinds (spec section 9): source_assets, jobs, packages,
revisions, evaluation_reports. Packages are stored per revision and never overwritten."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Job, LLMCall, ProductionPackage, QAReport, Revision, TraceEvent
from ..models.images import GeneratedImage
from ..models.package import utcnow


class Repository(Protocol):
    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None: ...
    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None: ...
    def get_source_assets(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]: ...
    def save_job(self, job: Job) -> None: ...
    def get_job(self, job_id: str) -> Job | None: ...
    def list_jobs(self, limit: int = 50, include_archived: bool = True) -> list[Job]: ...
    def list_archive_candidates(self) -> list[Job]: ...
    def list_active_jobs(self) -> list[Job]: ...
    def save_package(self, pkg: ProductionPackage, published: bool) -> None: ...
    def get_package(
        self, package_id: str, revision_id: str | None = None
    ) -> ProductionPackage | None: ...
    def save_revision(self, rev: Revision) -> None: ...
    def get_revision(self, revision_id: str) -> Revision | None: ...
    def save_evaluation_report(self, package_id: str, revision_id: str, r: QAReport) -> None: ...
    # Trace (live progress + debugging)
    def add_event(self, event: TraceEvent) -> int: ...
    def list_events(
        self, job_id: str, after_seq: int = 0, limit: int = 1000
    ) -> list[TraceEvent]: ...
    def save_llm_call(self, call: LLMCall) -> None: ...
    def get_llm_call(self, call_id: str) -> LLMCall | None: ...
    def list_llm_calls(self, job_id: str) -> list[LLMCall]: ...
    def save_image(self, image: GeneratedImage) -> None: ...
    def get_image(self, image_id: str) -> GeneratedImage | None: ...
    def list_images(
        self, package_id: str, revision_id: str | None = None
    ) -> list[GeneratedImage]: ...
    def claim_next_image(self) -> GeneratedImage | None: ...
    def requeue_running_images(self, older_than_s: float = 0.0) -> int: ...


class InMemoryRepository:
    """Process-local store for development and tests. Replace with a database for prod."""

    def __init__(self) -> None:
        self.source_assets: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, Job] = {}
        # package_id -> revision_id -> package.
        self.packages: dict[str, dict[str, ProductionPackage]] = {}
        # `published` points at the latest revision that passed hard checks; `latest_draft`
        # at the newest revision of any status. Drafts never displace a published revision.
        self.published: dict[str, str] = {}
        self.latest_draft: dict[str, str] = {}
        self.revisions: dict[str, Revision] = {}
        self.evaluation_reports: dict[tuple[str, str], QAReport] = {}
        self.events: list[TraceEvent] = []
        self.llm_calls: dict[str, LLMCall] = {}
        self.images: dict[str, GeneratedImage] = {}

    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None:
        self.source_assets[asset_id] = meta

    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.source_assets.get(asset_id)

    def get_source_assets(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {i: self.source_assets[i] for i in asset_ids if i in self.source_assets}

    def save_job(self, job: Job) -> None:
        self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 50, include_archived: bool = True) -> list[Job]:
        """Newest first."""
        jobs = [j for j in self.jobs.values() if include_archived or not j.archived]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    def list_archive_candidates(self) -> list[Job]:
        """Jobs not archived whose language isn't en-US (see archive.py)."""
        return [
            j
            for j in self.jobs.values()
            if not j.archived and j.request.options.language != "en-US"
        ]

    def list_active_jobs(self) -> list[Job]:
        """Jobs not yet finished (queued or running), oldest first."""
        active = [j for j in self.jobs.values() if j.status in ("queued", "running")]
        return sorted(active, key=lambda j: j.created_at)

    def save_package(self, pkg: ProductionPackage, published: bool) -> None:
        revs = self.packages.setdefault(pkg.package_id, {})
        if pkg.revision_id in revs:
            raise ValueError(f"revision {pkg.revision_id} already stored; revisions are immutable")
        revs[pkg.revision_id] = pkg.model_copy(deep=True)
        self.latest_draft[pkg.package_id] = pkg.revision_id
        if published:
            self.published[pkg.package_id] = pkg.revision_id

    def get_package(
        self, package_id: str, revision_id: str | None = None
    ) -> ProductionPackage | None:
        revs = self.packages.get(package_id)
        if not revs:
            return None
        # Default: latest published revision; a package that never passed (a first-run
        # needs_revision draft) falls back to its newest draft so it can be inspected/revised.
        rid = revision_id or self.published.get(package_id) or self.latest_draft[package_id]
        pkg = revs.get(rid)
        return pkg.model_copy(deep=True) if pkg else None

    def save_revision(self, rev: Revision) -> None:
        self.revisions[rev.revision_id] = rev

    def get_revision(self, revision_id: str) -> Revision | None:
        return self.revisions.get(revision_id)

    def save_evaluation_report(self, package_id: str, revision_id: str, r: QAReport) -> None:
        self.evaluation_reports[(package_id, revision_id)] = r

    def add_event(self, event: TraceEvent) -> int:
        seq = len(self.events) + 1
        self.events.append(event.model_copy(update={"seq": seq}))
        return seq

    def list_events(self, job_id: str, after_seq: int = 0, limit: int = 1000) -> list[TraceEvent]:
        return [e for e in self.events if e.job_id == job_id and e.seq > after_seq][:limit]

    def save_llm_call(self, call: LLMCall) -> None:
        self.llm_calls[call.call_id] = call.model_copy(deep=True)

    def get_llm_call(self, call_id: str) -> LLMCall | None:
        call = self.llm_calls.get(call_id)
        return call.model_copy(deep=True) if call else None

    def list_llm_calls(self, job_id: str) -> list[LLMCall]:
        calls = [c for c in self.llm_calls.values() if c.job_id == job_id]
        return [
            c.model_copy(update={"request": {}, "output": "", "output_chars": len(c.output)})
            for c in sorted(calls, key=lambda c: (c.started_at, c.call_id))
        ]

    # Generated images ---------------------------------------------------

    def save_image(self, image: GeneratedImage) -> None:
        self.images[image.image_id] = image.model_copy(deep=True)

    def get_image(self, image_id: str) -> GeneratedImage | None:
        img = self.images.get(image_id)
        return img.model_copy(deep=True) if img else None

    def list_images(self, package_id: str, revision_id: str | None = None) -> list[GeneratedImage]:
        return [
            i.model_copy(deep=True)
            for i in sorted(self.images.values(), key=lambda i: (i.created_at, i.image_id))
            if i.package_id == package_id and revision_id in (None, i.revision_id)
        ]

    def claim_next_image(self) -> GeneratedImage | None:
        """The oldest pending image, marked running (one worker processes images)."""
        pending = [i for i in self.images.values() if i.status == "pending"]
        if not pending:
            return None
        img = min(pending, key=lambda i: (i.created_at, i.image_id))
        img.status, img.started_at = "running", utcnow()
        return img.model_copy(deep=True)

    def requeue_running_images(self, older_than_s: float = 0.0) -> int:
        """Queue again images left running for longer than older_than_s (interrupted)."""
        n = 0
        cutoff = utcnow().timestamp() - older_than_s
        for img in self.images.values():
            started = img.started_at.timestamp() if img.started_at else 0.0
            if img.status == "running" and started <= cutoff:
                img.status, img.started_at, n = "pending", None, n + 1
        return n


def __getattr__(name: str) -> Any:
    # Lazy so importing the store package never touches sqlite unless asked.
    if name == "SqliteRepository":
        from .sqlite import SqliteRepository

        return SqliteRepository
    raise AttributeError(name)


__all__ = ["InMemoryRepository", "Repository", "SqliteRepository"]
