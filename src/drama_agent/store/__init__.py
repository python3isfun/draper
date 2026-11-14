"""Storage for the five V1 object kinds (spec section 9): source_assets, jobs, packages,
revisions, evaluation_reports. Packages are stored per revision and never overwritten."""

from __future__ import annotations

from typing import Any, Protocol

from ..experiment import ExpSession, ExpTurn
from ..models import Job, LLMCall, ProductionPackage, QAReport, Revision, TraceEvent
from ..models.images import DesignOption, GeneratedImage
from ..models.package import utcnow


class Repository(Protocol):
    def save_source_asset(self, asset_id: str, meta: dict[str, Any]) -> None: ...
    def get_source_asset(self, asset_id: str) -> dict[str, Any] | None: ...
    def get_source_assets(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]: ...
    def save_job(self, job: Job) -> None: ...
    def get_job(self, job_id: str) -> Job | None: ...
    def list_jobs(self, limit: int = 50, include_archived: bool = True) -> list[Job]: ...
    def list_archive_candidates(self) -> list[Job]: ...
    def jobs_for_package(self, package_id: str) -> list[Job]: ...
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
    def set_selected(self, image_id: str) -> bool: ...
    def move_selection(self, from_id: str, to_id: str) -> bool: ...
    def save_option(self, option: DesignOption) -> None: ...
    def get_option(self, option_id: str) -> DesignOption | None: ...
    def list_options(
        self, package_id: str, revision_id: str | None = None
    ) -> list[DesignOption]: ...
    def requeue_running_images(self, older_than_s: float = 0.0) -> int: ...
    # Experiment chat sessions
    def save_exp_session(self, session: ExpSession) -> None: ...
    def get_exp_session(self, session_id: str) -> ExpSession | None: ...
    def list_exp_sessions(self) -> list[ExpSession]: ...
    def delete_exp_session(self, session_id: str) -> None: ...
    def save_exp_turn(self, turn: ExpTurn) -> None: ...
    def get_exp_turn(self, turn_id: str) -> ExpTurn | None: ...
    def list_exp_turns(self, session_id: str) -> list[ExpTurn]: ...
    def fail_unfinished_exp_turns(self, error: str) -> int: ...
    def asset_in_use(self, asset_id: str) -> bool: ...
    def delete_source_asset(self, asset_id: str) -> None: ...


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
        self.options: dict[str, DesignOption] = {}
        self.exp_sessions: dict[str, ExpSession] = {}
        self.exp_turns: dict[str, ExpTurn] = {}

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

    def jobs_for_package(self, package_id: str) -> list[Job]:
        """Jobs that made this package, newest first."""
        jobs = [j for j in self.jobs.values() if j.package_id == package_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

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

    def set_selected(self, image_id: str) -> bool:
        """Pick this image for its deliverable and unpick the others."""
        return self._select(image_id, None)

    def move_selection(self, from_id: str, to_id: str) -> bool:
        """Move the pick from `from_id` to `to_id` only if `from_id` is still the pick."""
        return self._select(to_id, from_id)

    def _select(self, image_id: str, only_if: str | None) -> bool:
        target = self.images.get(image_id)
        if target is None:
            return False
        same = [
            i
            for i in self.images.values()
            if (i.package_id, i.revision_id, i.requirement_id, i.deliverable_id)
            == (target.package_id, target.revision_id, target.requirement_id, target.deliverable_id)
        ]
        if only_if is not None and not any(i.image_id == only_if and i.selected for i in same):
            return False
        for i in same:
            i.selected = i.image_id == image_id
        return True

    def save_option(self, option: DesignOption) -> None:
        self.options[option.option_id] = option.model_copy(deep=True)

    def get_option(self, option_id: str) -> DesignOption | None:
        opt = self.options.get(option_id)
        return opt.model_copy(deep=True) if opt else None

    def list_options(self, package_id: str, revision_id: str | None = None) -> list[DesignOption]:
        return [
            o.model_copy(deep=True)
            for o in sorted(self.options.values(), key=lambda o: (o.created_at, o.option_id))
            if o.package_id == package_id and revision_id in (None, o.revision_id)
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

    # Experiment chat sessions ------------------------------------------------

    def save_exp_session(self, session: ExpSession) -> None:
        self.exp_sessions[session.session_id] = session.model_copy(deep=True)

    def get_exp_session(self, session_id: str) -> ExpSession | None:
        s = self.exp_sessions.get(session_id)
        return s.model_copy(deep=True) if s else None

    def list_exp_sessions(self) -> list[ExpSession]:
        """Most recently active first."""
        return [
            s.model_copy(deep=True)
            for s in sorted(self.exp_sessions.values(), key=lambda s: s.updated_at, reverse=True)
        ]

    def delete_exp_session(self, session_id: str) -> None:
        self.exp_sessions.pop(session_id, None)
        for tid in [t.turn_id for t in self.exp_turns.values() if t.session_id == session_id]:
            del self.exp_turns[tid]

    def save_exp_turn(self, turn: ExpTurn) -> None:
        self.exp_turns[turn.turn_id] = turn.model_copy(deep=True)

    def get_exp_turn(self, turn_id: str) -> ExpTurn | None:
        t = self.exp_turns.get(turn_id)
        return t.model_copy(deep=True) if t else None

    def list_exp_turns(self, session_id: str) -> list[ExpTurn]:
        return [
            t.model_copy(deep=True)
            for t in sorted(self.exp_turns.values(), key=lambda t: t.index)
            if t.session_id == session_id
        ]

    def fail_unfinished_exp_turns(self, error: str) -> int:
        n = 0
        for t in self.exp_turns.values():
            if t.status in ("pending", "running"):
                t.status, t.error, t.finished_at, n = "failed", error, utcnow(), n + 1
        return n

    def asset_in_use(self, asset_id: str) -> bool:
        """Whether a job, a reference image or a chat message uses this upload."""
        return (
            any(asset_id in j.model_dump_json() for j in self.jobs.values())
            or any(asset_id in i.reference_ids for i in self.images.values())
            or any(asset_id in t.attachments for t in self.exp_turns.values())
        )

    def delete_source_asset(self, asset_id: str) -> None:
        self.source_assets.pop(asset_id, None)


def __getattr__(name: str) -> Any:
    # Lazy so importing the store package never touches sqlite unless asked.
    if name == "SqliteRepository":
        from .sqlite import SqliteRepository

        return SqliteRepository
    raise AttributeError(name)


__all__ = ["InMemoryRepository", "Repository", "SqliteRepository"]
