"""Storage for the five V1 object kinds (spec section 9): source_assets, jobs, packages,
revisions, evaluation_reports. Packages are stored per revision and never overwritten."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Job, ProductionPackage, QAReport, Revision


class Repository(Protocol):
    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None: ...
    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None: ...
    def save_job(self, job: Job) -> None: ...
    def get_job(self, job_id: str) -> Job | None: ...
    def list_jobs(self, limit: int = 50) -> list[Job]: ...
    def list_active_jobs(self) -> list[Job]: ...
    def save_package(self, pkg: ProductionPackage, published: bool) -> None: ...
    def get_package(
        self, package_id: str, revision_id: str | None = None
    ) -> ProductionPackage | None: ...
    def save_revision(self, rev: Revision) -> None: ...
    def get_revision(self, revision_id: str) -> Revision | None: ...
    def save_evaluation_report(self, package_id: str, revision_id: str, r: QAReport) -> None: ...


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

    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None:
        self.source_assets[asset_id] = meta

    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.source_assets.get(asset_id)

    def save_job(self, job: Job) -> None:
        self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        """Newest first."""
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

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


def __getattr__(name: str) -> Any:
    # Lazy so importing the store package never touches sqlite unless asked.
    if name == "SqliteRepository":
        from .sqlite import SqliteRepository

        return SqliteRepository
    raise AttributeError(name)


__all__ = ["InMemoryRepository", "Repository", "SqliteRepository"]
