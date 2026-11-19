"""Storage for the five V1 object kinds (spec section 9): source_assets, jobs, packages,
revisions, evaluation_reports. Packages are stored per revision and never overwritten."""

from __future__ import annotations

from typing import Any, Protocol

from ..experiment import ExpSession, ExpTurn
from ..library.models import AssetImage, Casting, LibraryAsset
from ..models import Job, LLMCall, ProductionPackage, QAReport, Revision, TraceEvent
from ..models.images import DesignOption, GeneratedImage
from ..models.package import utcnow
from ..shot_frames import PromptVersion, ShotPlan, ShotTake, approval_changes
from ..shot_reading import SceneReading


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
    def replace_package(self, pkg: ProductionPackage) -> None: ...
    def get_package(
        self, package_id: str, revision_id: str | None = None
    ) -> ProductionPackage | None: ...
    def save_revision(self, rev: Revision) -> None: ...
    def get_revision(self, revision_id: str) -> Revision | None: ...
    def list_revisions(self, package_id: str | None = None) -> list[Revision]: ...
    def carry_forward(self, package_id: str, from_revision: str, to_revision: str) -> int: ...
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
    def delete_images(self, image_ids: list[str]) -> None: ...
    def delete_option(self, option_id: str) -> None: ...
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
    def allocate_exp_labels(self, session_id: str, n: int) -> list[int]: ...
    def asset_in_use(self, asset_id: str) -> bool: ...
    def image_ids(self, refs: list[str]) -> dict[str, int]: ...
    def image_ref(self, gid: int) -> str | None: ...
    def get_story_rating(self, package_id: str) -> str | None: ...
    def set_story_rating(self, package_id: str, rating: str) -> None: ...
    def delete_source_asset(self, asset_id: str) -> None: ...
    def save_shot_take(self, take: ShotTake) -> None: ...
    def get_shot_take(self, take_id: str) -> ShotTake | None: ...
    def list_shot_takes(self, package_id: str, revision_id: str) -> list[ShotTake]: ...
    def delete_shot_take(self, take_id: str) -> None: ...
    def approve_shot_draw(self, take_id: str, n: int | None) -> ShotTake: ...
    def fail_unfinished_shot_takes(self, error: str) -> int: ...
    def save_shot_plan(self, plan: ShotPlan) -> None: ...
    def get_shot_plan(self, package_id: str, revision_id: str, shot_id: str) -> ShotPlan | None: ...
    def list_shot_plans(self, package_id: str, revision_id: str) -> list[ShotPlan]: ...
    def fail_unfinished_shot_plans(self, error: str) -> int: ...
    def add_prompt_version(self, version: PromptVersion) -> PromptVersion: ...
    def save_scene_reading(self, reading: SceneReading) -> None: ...
    def get_scene_reading(
        self, package_id: str, revision_id: str, scene_id: str
    ) -> SceneReading | None: ...
    def list_scene_readings(self, package_id: str, revision_id: str) -> list[SceneReading]: ...
    def fail_unfinished_scene_readings(self, error: str) -> int: ...
    # The asset library (library/): characters, sets, their looks, states and items
    def list_package_ids(self) -> list[str]: ...
    def save_library_asset(self, asset: LibraryAsset) -> None: ...
    def get_library_asset(self, asset_id: str) -> LibraryAsset | None: ...
    def list_library_assets(
        self, kind: str | None = None, parent_id: str | None = None
    ) -> list[LibraryAsset]: ...
    def save_casting(self, casting: Casting) -> None: ...
    def list_casting(
        self, package_id: str | None = None, asset_id: str | None = None
    ) -> list[Casting]: ...
    def link_asset_image(self, link: AssetImage) -> None: ...
    def list_asset_images(
        self, asset_id: str | None = None, package_id: str | None = None
    ) -> list[AssetImage]: ...
    def list_prompt_versions(self, package_id: str, revision_id: str) -> list[PromptVersion]: ...


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
        self.exp_next_label: dict[str, int] = {}
        self.gids: dict[str, int] = {}  # global image numbers (image_ids.py)
        self.ratings: dict[str, str] = {}  # stories' changed ratings
        self.shot_takes: dict[str, ShotTake] = {}
        self.shot_plans: dict[tuple[str, str, str], ShotPlan] = {}
        self.prompt_versions: list[PromptVersion] = []
        self.scene_readings: dict[tuple[str, str, str], SceneReading] = {}
        self.library: dict[str, LibraryAsset] = {}
        self.casting: dict[tuple[str, str], Casting] = {}
        self.asset_images: dict[str, AssetImage] = {}

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

    def replace_package(self, pkg: ProductionPackage) -> None:
        """Overwrite a stored revision: hand edits only (revisions are otherwise immutable)."""
        revs = self.packages.get(pkg.package_id, {})
        if pkg.revision_id not in revs:
            raise KeyError(pkg.revision_id)
        revs[pkg.revision_id] = pkg.model_copy(deep=True)

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

    def list_revisions(self, package_id: str | None = None) -> list[Revision]:
        """Revisions, oldest first (all packages when package_id is None)."""
        return [r for r in self.revisions.values() if package_id in (None, r.package_id)]

    def carry_forward(self, package_id: str, from_revision: str, to_revision: str) -> int:
        """Move a story's design pictures, design options, keyframe takes, prompt versions,
        cut plans, scene readings and library picture links from one revision to the next
        (a published revise run). Ids stay the same, so picks and references keep working;
        what the revision changed is then judged out of date by its fingerprints. Returns
        the number of rows moved."""
        moved = 0
        stores: list[Any] = [self.images, self.options, self.shot_takes, self.asset_images]
        for store in stores:
            for key, row in list(store.items()):
                if row.package_id == package_id and row.revision_id == from_revision:
                    store[key] = row.model_copy(update={"revision_id": to_revision})
                    moved += 1
        for keyed in (self.shot_plans, self.scene_readings):
            for key, row in list(keyed.items()):
                if row.package_id == package_id and row.revision_id == from_revision:
                    del keyed[key]
                    keyed[(key[0], to_revision, key[2])] = row.model_copy(
                        update={"revision_id": to_revision}
                    )
                    moved += 1
        for i, pv in enumerate(self.prompt_versions):
            if pv.package_id == package_id and pv.revision_id == from_revision:
                self.prompt_versions[i] = pv.model_copy(update={"revision_id": to_revision})
                moved += 1
        return moved

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

    def delete_images(self, image_ids: list[str]) -> None:
        for i in image_ids:
            self.images.pop(i, None)

    def delete_option(self, option_id: str) -> None:
        self.options.pop(option_id, None)

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
        self.exp_next_label.pop(session_id, None)
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

    def allocate_exp_labels(self, session_id: str, n: int) -> list[int]:
        """The next n image numbers of a chat (1, 2, ...), never handed out twice."""
        start = self.exp_next_label.get(session_id, 1)
        self.exp_next_label[session_id] = start + n
        return list(range(start, start + n))

    def image_ids(self, refs: list[str]) -> dict[str, int]:
        for r in refs:
            self.gids.setdefault(r, len(self.gids) + 1)
        return {r: self.gids[r] for r in refs}

    def image_ref(self, gid: int) -> str | None:
        return next((r for r, g in self.gids.items() if g == gid), None)

    def get_story_rating(self, package_id: str) -> str | None:
        return self.ratings.get(package_id)

    def set_story_rating(self, package_id: str, rating: str) -> None:
        self.ratings[package_id] = rating

    def asset_in_use(self, asset_id: str) -> bool:
        """Whether a job, a reference image or a chat message uses this upload."""
        return (
            any(asset_id in j.model_dump_json() for j in self.jobs.values())
            or any(asset_id in i.reference_ids for i in self.images.values())
            or any(asset_id in t.attachments for t in self.exp_turns.values())
        )

    def delete_source_asset(self, asset_id: str) -> None:
        self.source_assets.pop(asset_id, None)

    def save_shot_take(self, take: ShotTake) -> None:
        """An update never changes `approved` (see SqliteRepository.save_shot_take)."""
        old = self.shot_takes.get(take.take_id)
        update = {"approved": old.approved} if old is not None else {}
        self.shot_takes[take.take_id] = take.model_copy(update=update, deep=True)

    def get_shot_take(self, take_id: str) -> ShotTake | None:
        t = self.shot_takes.get(take_id)
        return t.model_copy(deep=True) if t else None

    def list_shot_takes(self, package_id: str, revision_id: str) -> list[ShotTake]:
        mine = [t for t in self.shot_takes.values()
                if t.package_id == package_id and t.revision_id == revision_id]  # fmt: skip
        return [t.model_copy(deep=True) for t in sorted(mine, key=lambda t: t.created_at)]

    def delete_shot_take(self, take_id: str) -> None:
        self.shot_takes.pop(take_id, None)

    def approve_shot_draw(self, take_id: str, n: int | None) -> ShotTake:
        take = self.shot_takes[take_id].model_copy(deep=True)
        others = [t.model_copy(deep=True) for t in self.shot_takes.values()
                  if t.take_id != take_id and (t.package_id, t.revision_id, t.shot_id)
                  == (take.package_id, take.revision_id, take.shot_id)]  # fmt: skip
        for t in approval_changes(take, n, others):
            self.shot_takes[t.take_id] = t
        return take.model_copy(deep=True)

    def save_shot_plan(self, plan: ShotPlan) -> None:
        key = (plan.package_id, plan.revision_id, plan.shot_id)
        self.shot_plans[key] = plan.model_copy(deep=True)

    def get_shot_plan(self, package_id: str, revision_id: str, shot_id: str) -> ShotPlan | None:
        p = self.shot_plans.get((package_id, revision_id, shot_id))
        return p.model_copy(deep=True) if p else None

    def list_shot_plans(self, package_id: str, revision_id: str) -> list[ShotPlan]:
        return [p.model_copy(deep=True) for (pid, rid, _), p in self.shot_plans.items()
                if (pid, rid) == (package_id, revision_id)]  # fmt: skip

    def save_scene_reading(self, reading: SceneReading) -> None:
        key = (reading.package_id, reading.revision_id, reading.scene_id)
        self.scene_readings[key] = reading.model_copy(deep=True)

    def get_scene_reading(
        self, package_id: str, revision_id: str, scene_id: str
    ) -> SceneReading | None:
        r = self.scene_readings.get((package_id, revision_id, scene_id))
        return r.model_copy(deep=True) if r else None

    def list_scene_readings(self, package_id: str, revision_id: str) -> list[SceneReading]:
        return [r.model_copy(deep=True) for (pid, rid, _), r in self.scene_readings.items()
                if (pid, rid) == (package_id, revision_id)]  # fmt: skip

    def fail_unfinished_scene_readings(self, error: str) -> int:
        n = 0
        for r in self.scene_readings.values():
            if r.busy:
                r.status, r.error, n = "failed", error, n + 1
        return n

    # The asset library ---------------------------------------------------

    def list_package_ids(self) -> list[str]:
        return list(self.packages)

    def save_library_asset(self, asset: LibraryAsset) -> None:
        self.library[asset.asset_id] = asset.model_copy(deep=True)

    def get_library_asset(self, asset_id: str) -> LibraryAsset | None:
        a = self.library.get(asset_id)
        return a.model_copy(deep=True) if a else None

    def list_library_assets(
        self, kind: str | None = None, parent_id: str | None = None
    ) -> list[LibraryAsset]:
        return [a.model_copy(deep=True) for a in self.library.values()
                if (kind is None or a.kind == kind)
                and (parent_id is None or a.parent_id == parent_id)]  # fmt: skip

    def save_casting(self, casting: Casting) -> None:
        self.casting[(casting.package_id, casting.story_id)] = casting.model_copy()

    def list_casting(
        self, package_id: str | None = None, asset_id: str | None = None
    ) -> list[Casting]:
        return [c.model_copy() for c in self.casting.values()
                if (package_id is None or c.package_id == package_id)
                and (asset_id is None or c.asset_id == asset_id)]  # fmt: skip

    def link_asset_image(self, link: AssetImage) -> None:
        self.asset_images[link.image_id] = link.model_copy()

    def list_asset_images(
        self, asset_id: str | None = None, package_id: str | None = None
    ) -> list[AssetImage]:
        return [x.model_copy() for x in self.asset_images.values()
                if (asset_id is None or x.asset_id == asset_id)
                and (package_id is None or x.package_id == package_id)]  # fmt: skip

    def add_prompt_version(self, version: PromptVersion) -> PromptVersion:
        key = (version.package_id, version.revision_id, version.shot_id, version.sub_shot)
        last = max((v.v for v in self.prompt_versions
                    if (v.package_id, v.revision_id, v.shot_id, v.sub_shot) == key), default=0)  # fmt: skip
        version = version.model_copy(update={"v": last + 1}, deep=True)
        self.prompt_versions.append(version)
        return version.model_copy(deep=True)

    def list_prompt_versions(self, package_id: str, revision_id: str) -> list[PromptVersion]:
        mine = [
            v
            for v in self.prompt_versions
            if (v.package_id, v.revision_id) == (package_id, revision_id)
        ]
        return [
            v.model_copy(deep=True)
            for v in sorted(mine, key=lambda v: (v.shot_id, v.sub_shot, v.v))
        ]

    def fail_unfinished_shot_plans(self, error: str) -> int:
        n = 0
        for p in self.shot_plans.values():
            if p.busy:
                p.status, p.error, n = "failed", error, n + 1
        return n

    def fail_unfinished_shot_takes(self, error: str) -> int:
        n = 0
        for t in self.shot_takes.values():
            if t.busy:
                t.status, t.error, t.finished_at, n = "failed", error, utcnow(), n + 1
        return n


def __getattr__(name: str) -> Any:
    # Lazy so importing the store package never touches sqlite unless asked.
    if name == "SqliteRepository":
        from .sqlite import SqliteRepository

        return SqliteRepository
    raise AttributeError(name)


__all__ = ["InMemoryRepository", "Repository", "SqliteRepository"]
