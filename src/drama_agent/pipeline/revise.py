"""The revise run (phase 5 of docs/entity-reorganization.md): a targeted revision of a
published story.

A revision request names what to change (target_ids), how (instruction) and what must not
change (locked_ids). analyze_impact (impact.py) works out what may change with the targets.
The run then:

1. re-runs only the pipeline steps that own the targets, and the steps that depend on them
   (a story fact: outline, then script and storyboard for the scenes it touches; a look or
   set state: its design sheet; a scene or line: script and storyboard; a shot: storyboard),
   each shown its current output and told what may change;
2. puts back everything outside the scope, and every locked object, from the base revision
   (the program enforces the scope; the model is only asked to respect it);
3. gates the result with the step checks, the hard checks and check_locks;
4. publishes it as a new revision of the same package, then carries the story's work
   forward to it: design pictures, design options, keyframe takes, prompt versions, cut
   plans, scene readings. What the revision changed is out of date by the usual rules
   (design text fingerprints for pictures, plans and keyframes; a "Revised" change-log
   entry for shots whose action changed); everything else stays current.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from ..models import Issue, ProductionPackage, Revision, RevisionRequest
from ..models.package import STAGES, ChangeLogEntry, utcnow
from ..pace import story_pace
from ..tracing import NullRecorder, Tracer
from ..validation import PackageIndex
from ..validation import hard_checks as hc
from ..validation.index import design_entries, lockable_objects
from .impact import _scene_of
from .steps import (
    OUTPUT_RULES,
    STEPS,
    PipelineState,
    Step,
    finish_storyboard,
    mode_rules,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Added to each re-run step's instructions.
REVISE_RULES = (
    "Revision run: this story is already written and published. context.revision says what "
    "to change: instruction (the user's request), target_ids (what they asked to change), "
    "impacted_ids (what may change along with them) and locked_ids (must stay exactly as "
    "they are). This step's current output is in the context (its own field, or "
    "context.revision.current). Return this step's complete output: copy everything "
    "outside target_ids and impacted_ids exactly as it is, keep every existing ID, and "
    "change only what the instruction needs. Changes outside that scope are discarded."
)

# The change-log note of a revise run; shot staleness looks for it (shots/staleness.py).
REVISED_NOTE = "Revised"

_STEPS = {s.name: s for s in STEPS}
_SHEET_OF = {"character": "cast", "location": "sets", "prop": "props"}
_SHEET_FIELD = {"cast": "cast_sheet", "sets": "set_sheet", "props": "prop_sheet"}

# What still counts as work in flight for a story (moving its rows mid-flight would let it
# save under the old revision).
_BUSY_TAKES = ("pending", "writing", "drawing", "checking")


# ---------------------------------------------------------------------------
# Scope: what may change


@dataclass
class Scope:
    open_ids: set[str]  # targets and what may change with them (minus locked)
    locked: set[str]
    scenes: set[str]  # scenes whose content may change (a scene, beat or line in scope)
    episodes: set[str]  # episodes whose own fields or scenes may change
    # Scenes of the shots in scope: their shots may be re-cut (a new shot is allowed there)
    # and their episodes' timing re-totalled, but their other shots stay as they are.
    shot_scenes: set[str]
    shot_episodes: set[str]

    def opens(self, oid: str) -> bool:
        return oid in self.open_ids


def make_scope(base: ProductionPackage, rev: Revision) -> Scope:
    req = rev.request
    assert req is not None
    idx = PackageIndex.build(base)
    locked = set(req.locked_ids) | set(base.locked_ids)
    open_ids = set(req.target_ids) | set(rev.impacted_ids)
    if req.visual_change:
        # A visual change to a character, place or prop reaches its looks or states.
        bible = base.story_bible
        for t in req.target_ids:
            open_ids |= {x.look_id for x in bible.looks if x.character_id == t}
            open_ids |= {x.location_state_id for x in bible.location_states if x.location_id == t}
            open_ids |= {x.prop_state_id for x in bible.prop_states if x.prop_id == t}
    open_ids -= locked
    scenes = {sc for x in open_ids if x not in idx.shots
              and (sc := _scene_of(base, idx, x)) is not None} - locked  # fmt: skip
    shot_scenes = {idx.shots[x].scene_id for x in open_ids if x in idx.shots}
    scene_episode = {s.scene_id: ep.episode_id for ep in base.episodes for s in ep.scenes}
    episodes = ({e for e in open_ids if e in idx.episodes}
                | {scene_episode[s] for s in scenes if s in scene_episode}) - locked  # fmt: skip
    shot_episodes = {scene_episode[s] for s in shot_scenes if s in scene_episode} - locked
    return Scope(open_ids=open_ids, locked=locked, scenes=scenes, episodes=episodes,
                 shot_scenes=shot_scenes, shot_episodes=shot_episodes)  # fmt: skip


def plan_steps(base: ProductionPackage, req: RevisionRequest, scope: Scope) -> list[str]:
    """The steps to re-run, in pipeline order (review always runs last)."""
    idx = PackageIndex.build(base)
    reqs = base.asset_requirements.by_id()
    steps: set[str] = {"review"}
    for t in req.target_ids:
        for kind, table in (("character", idx.characters), ("location", idx.locations),
                            ("prop", idx.props)):  # fmt: skip
            if t in table:
                steps.add("outline")
                if req.visual_change:
                    steps.add(_SHEET_OF[kind])
        if t in idx.looks:
            steps.add("cast")
        elif t in idx.location_states:
            steps.add("sets")
        elif t in idx.prop_states:
            steps.add("props")
        elif t in idx.episodes or t in idx.scenes or t in idx.beats or t in idx.dialogue:
            steps.add("script")
        elif t in idx.shots or t in reqs:
            steps.add("storyboard")
    if "outline" in steps and scope.scenes:
        steps.add("script")
    if "script" in steps:
        steps.add("storyboard")
    return [s for s in STAGES if s in steps]


# ---------------------------------------------------------------------------
# Putting back what may not change


def _merge(
    new: list[T],
    base: list[T],
    key: Callable[[Any], str],
    keep: Callable[[Any], bool],
    allow_new: Callable[[Any], bool] = lambda _x: True,
) -> list[T]:
    """The model's list with each base entry that `keep`s replaced by the base one (and put
    back where the model dropped it); new entries only where `allow_new`."""
    base_by = {key(x): x for x in base}
    out: list[T] = []
    for x in new:
        b = base_by.get(key(x))
        if b is not None and keep(b):
            out.append(b.model_copy(deep=True))
        elif b is not None or allow_new(x):
            out.append(x)
    have = {key(x) for x in out}
    for pos, b in enumerate(base):
        if key(b) in have or not keep(b):
            continue
        before = {key(y) for y in base[:pos]}
        at = max((j + 1 for j, y in enumerate(out) if key(y) in before), default=0)
        out.insert(at, b.model_copy(deep=True))
        have.add(key(b))
    return out


def _by(attr: str) -> Callable[[Any], str]:
    return lambda x: getattr(x, attr)


def restore(step: str, state: PipelineState, base: ProductionPackage, scope: Scope) -> None:
    """Put back, from the base revision, what this step's output may not change."""
    closed = lambda attr: lambda x: not scope.opens(getattr(x, attr))
    if step == "outline":
        bible, old = state.story_bible, base.story_bible
        assert bible is not None
        for field, attr in (("characters", "character_id"), ("looks", "look_id"),
                            ("locations", "location_id"),
                            ("location_states", "location_state_id"), ("props", "prop_id"),
                            ("prop_states", "prop_state_id")):  # fmt: skip
            setattr(bible, field, _merge(getattr(bible, field), getattr(old, field),
                                         _by(attr), closed(attr)))  # fmt: skip
        bible.beat_outlines = _merge(
            bible.beat_outlines, old.beat_outlines, _by("episode_id"),
            lambda b: b.episode_id not in scope.episodes, lambda _x: False,
        )  # fmt: skip
    elif step == "script":
        state.episodes = _merge(
            state.episodes, base.episodes, _by("episode_id"),
            lambda e: e.episode_id not in scope.episodes, lambda _x: False,
        )  # fmt: skip
        old_eps = {e.episode_id: e for e in base.episodes}
        for ep in state.episodes:
            if ep.episode_id in scope.episodes and (old := old_eps.get(ep.episode_id)):
                ep.scenes = _merge(ep.scenes, old.scenes, _by("scene_id"),
                                   lambda s: s.scene_id not in scope.scenes)  # fmt: skip
        _restore_locked_lines(state, base, scope)
    elif step == "cast" and state.cast_sheet and base.cast_sheet:
        s, b = state.cast_sheet, base.cast_sheet
        s.characters = _merge(s.characters, b.characters, _by("character_id"),
                              closed("character_id"))  # fmt: skip
        s.looks = _merge(s.looks, b.looks, _by("look_id"), closed("look_id"))
    elif step == "sets" and state.set_sheet and base.set_sheet:
        s, b = state.set_sheet, base.set_sheet
        s.locations = _merge(s.locations, b.locations, _by("location_id"), closed("location_id"))
        s.states = _merge(s.states, b.states, _by("location_state_id"),
                          closed("location_state_id"))  # fmt: skip
    elif step == "props" and state.prop_sheet and base.prop_sheet:
        s, b = state.prop_sheet, base.prop_sheet
        s.props = _merge(s.props, b.props, _by("prop_id"), closed("prop_id"))
        s.states = _merge(s.states, b.states, _by("prop_state_id"), closed("prop_state_id"))
    elif step == "storyboard":
        state.shots = _merge(
            state.shots, base.shot_list, _by("shot_id"),
            lambda sh: sh.shot_id in scope.locked
            or (not scope.opens(sh.shot_id) and sh.scene_id not in scope.scenes),
            lambda sh: sh.scene_id in scope.scenes | scope.shot_scenes,
        )  # fmt: skip
        state.timing = _merge(
            state.timing, base.timing, _by("episode_id"),
            lambda t: t.episode_id not in scope.episodes | scope.shot_episodes,
        )  # fmt: skip


def _restore_locked_lines(state: PipelineState, base: ProductionPackage, scope: Scope) -> None:
    """A locked dialogue line inside a scene that may change keeps its exact text."""
    old = PackageIndex.build(base).dialogue
    for ep in state.episodes:
        for sc in ep.scenes:
            for beat in sc.beats:
                d = beat.dialogue
                if d is not None and d.dialogue_id in scope.locked and d.dialogue_id in old:
                    beat.dialogue = old[d.dialogue_id].model_copy(deep=True)


def changed_ids(base: ProductionPackage, new: ProductionPackage) -> list[str]:
    """The objects (and design-sheet entries) added, removed or changed."""
    out: set[str] = set()
    for a, b in ((lockable_objects(base), lockable_objects(new)),
                 (design_entries(base), design_entries(new))):  # fmt: skip
        for i in a.keys() | b.keys():
            if i not in a or i not in b or a[i].model_dump() != b[i].model_dump():
                out.add(i)
    return sorted(out)


def _kept_overrides(
    base: ProductionPackage, state: PipelineState, rev: Revision, scope: Scope
) -> dict[str, Any]:
    """The base revision's design briefs for the requirements the revision doesn't reach:
    not in scope, not made stale by it, and whose subject's design text did not change (a
    changed one is written again from the new sheets, or by the storyboard step)."""

    def dumped(entries: dict[str, BaseModel], i: str) -> dict[str, Any] | None:
        return entries[i].model_dump() if i in entries else None

    before, after = design_entries(base), design_entries(state.draft_package())
    edited = {i for i in before.keys() | after.keys() if dumped(before, i) != dumped(after, i)}
    reach = (set(rev.stale_requirement_ids) | scope.open_ids) - scope.locked
    return {
        r.requirement_id: r
        for r in base.asset_requirements.items
        if r.requirement_id not in reach
        and not any({s.entity_id, s.variant_id} & edited for s in r.subject_refs)
    }


def busy_work(repo: Any, package_id: str, revision_id: str) -> str | None:
    """What is still being made for a story revision, or None when it is idle."""
    if any(i.status == "running" for i in repo.list_images(package_id, revision_id)):
        return "design pictures are being drawn"
    if any(t.status in _BUSY_TAKES for t in repo.list_shot_takes(package_id, revision_id)):
        return "keyframes are being made"
    if any(p.busy for p in repo.list_shot_plans(package_id, revision_id)):
        return "cut plans are being written"
    if any(r.busy for r in repo.list_scene_readings(package_id, revision_id)):
        return "scenes are being read"
    return None


# ---------------------------------------------------------------------------
# The run


def coverage_in_scope(
    issues: list[Issue], pkg: ProductionPackage, rewritten: set[str]
) -> list[Issue]:
    """The issues a revision is held to: every check but coverage, and coverage only on
    the shots this revision wrote (`rewritten`: its changed ids) and the scenes those
    shots are in. A story from before the rule keeps its old shots, and a revision that
    didn't touch the storyboard isn't held to it."""
    scenes = {s.scene_id for s in pkg.shot_list if s.shot_id in rewritten}
    return [i for i in issues if i.check != "coverage"
            or i.object_id in rewritten or i.object_id in scenes]  # fmt: skip


class ReviseRunner:
    """Runs revisions in this process, one task each (like the keyframe runner)."""

    def __init__(
        self,
        repo: Any,
        orchestrator: Any,
        *,
        wait_s: float = 900.0,
        poll_s: float = 5.0,
        after_publish: Callable[[], None] | None = None,
    ) -> None:
        self.repo = repo
        self.orchestrator = orchestrator
        self.wait_s = wait_s  # how long publishing waits for pictures being drawn
        self.poll_s = poll_s
        self.after_publish = after_publish  # e.g. a library sync
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, package_id: str) -> bool:
        return any(
            not t.done() and (r := self.repo.get_revision(rid)) and r.package_id == package_id
            for rid, t in self._tasks.items()
        )

    def start(self, revision_id: str) -> None:
        task = asyncio.create_task(self.run(revision_id))
        self._tasks[revision_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(revision_id, None))

    def recover(self) -> int:
        """Revisions left queued or running by a stopped server: failed, to request again."""
        n = 0
        for rev in self.repo.list_revisions():
            if rev.status in ("queued", "running") and rev.request is not None:
                rev.status, rev.error = "failed", "interrupted (the server restarted)"
                rev.finished_at = utcnow()
                self.repo.save_revision(rev)
                n += 1
        return n

    async def run(self, revision_id: str) -> Revision:
        rev = self.repo.get_revision(revision_id)
        if rev is None or rev.request is None or rev.base_revision_id is None:
            raise KeyError(revision_id)
        jobs = self.repo.jobs_for_package(rev.package_id)
        trace = Tracer(self.repo, jobs[0].job_id) if jobs else None
        try:
            return await self._run(rev, trace)
        except asyncio.CancelledError:
            self._end(rev, "failed", error="cancelled")
            raise
        except Exception as exc:  # the revision must record any failure
            log.exception("revision %s failed", revision_id)
            self._end(rev, "failed", error=f"{type(exc).__name__}: {exc}")
            if trace:
                trace.event("revision_failed", f"Revision {revision_id} failed: {rev.error}")
            return rev

    def _end(self, rev: Revision, status: str, **fields: Any) -> None:
        rev.status = status  # type: ignore[assignment]
        for k, v in fields.items():
            setattr(rev, k, v)
        rev.finished_at = utcnow()
        self.repo.save_revision(rev)

    async def _run(self, rev: Revision, trace: Tracer | None) -> Revision:
        req = rev.request
        assert req is not None and rev.base_revision_id is not None
        base = self.repo.get_package(rev.package_id, rev.base_revision_id)
        if base is None:
            raise RuntimeError(f"base revision {rev.base_revision_id} is gone")
        scope = make_scope(base, rev)
        rev.steps = plan_steps(base, req, scope)
        rev.status = "running"
        self.repo.save_revision(rev)
        say = trace.event if trace else (lambda *_a, **_k: None)
        say("revision_started",
            f"Revision {rev.revision_id}: re-running {', '.join(rev.steps)} for "
            f"{', '.join(req.target_ids)}.")  # fmt: skip

        state = _state_from(base, rev.revision_id)
        # The story's rating as it is now: it may have been changed on its Overview.
        from ..design.setup import story_rating

        state.request.options.content_rating = story_rating(  # type: ignore[assignment]
            self.repo, rev.package_id
        )
        # And its pace, which may have been changed there too.
        state.request.options.pace = story_pace(self.repo, rev.package_id)
        content_ran = False
        for name in rev.steps:
            step = _STEPS[name]
            issues = await self._run_step(step, state, base, scope, rev, trace)
            if issues:
                return self._needs_revision(rev, base, state, issues, step=name, trace=trace)
            content_ran |= name != "review"
            if name in ("cast", "sets", "props", "outline") and "storyboard" not in rev.steps:
                # The requirements are derived from the sheets and the bible: derive again.
                finish_storyboard(state, base.asset_requirements.style_brief,
                                  _kept_overrides(base, state, rev, scope))  # fmt: skip

        new = state.draft_package()
        new.experiment = base.experiment
        # The request's locks hold for this run only; the story's own stay as they were.
        new.locked_ids = list(base.locked_ids)
        new.stale_ids = sorted(set(base.stale_ids) | set(rev.stale_requirement_ids))
        new.assumptions = [*base.assumptions, *new.assumptions]
        new.open_questions = [*base.open_questions, *new.open_questions]
        rev.changed_ids = changed_ids(base, new)
        new.change_log = [*base.change_log, ChangeLogEntry(
            revision_id=rev.revision_id, target_ids=list(req.target_ids),
            changed_ids=rev.changed_ids, note=f"{REVISED_NOTE}: {req.instruction}", at=utcnow(),
        )]  # fmt: skip
        locked_base = base.model_copy(update={"locked_ids": sorted(scope.locked)})
        final = hc.run_hard_checks(new, state.request.options) + hc.check_locks(locked_base, new)
        final = coverage_in_scope(final, new, set(rev.changed_ids) if "storyboard" in rev.steps
                                  else set())  # fmt: skip
        if final:
            return self._needs_revision(rev, base, state, final, step=None, trace=trace, pkg=new)

        if busy := await self._wait_idle(rev.package_id, base.revision_id):
            raise RuntimeError(f"not published: {busy} (waited {self.wait_s:.0f}s)")
        # Nothing awaits from here to the move, so no work can start in between.
        now = self.repo.get_package(rev.package_id, base.revision_id)
        if now is None or now.model_dump() != base.model_dump():
            raise RuntimeError(
                "not published: the story was edited by hand while the revision ran; "
                "request it again"
            )
        self.repo.save_package(new, published=True)
        self.repo.save_evaluation_report(new.package_id, new.revision_id, new.qa_report)
        moved = self.repo.carry_forward(new.package_id, base.revision_id, new.revision_id)
        for job in self.repo.jobs_for_package(new.package_id):
            if job.revision_id == base.revision_id:
                job.revision_id = new.revision_id
                job.updated_at = utcnow()
                self.repo.save_job(job)
        self._end(rev, "published", moved=moved)
        say("revision_published",
            f"Published revision {rev.revision_id}: {len(rev.changed_ids)} objects changed; "
            f"{moved} pictures, takes, plans and readings carried forward.",
            changed=rev.changed_ids[:50])  # fmt: skip
        if self.after_publish:
            try:
                self.after_publish()
            except Exception:  # the revision is published; a sync can run later
                log.warning("after-publish hook failed", exc_info=True)
        return rev

    async def _run_step(
        self,
        step: Step,
        state: PipelineState,
        base: ProductionPackage,
        scope: Scope,
        rev: Revision,
        trace: Tracer | None,
    ) -> list[Issue]:
        """One re-run step with its bounded retries; returns the issues left (none: passed)."""
        req = rev.request
        assert req is not None
        from ..llm import GenerationRequest

        orch, client = self.orchestrator, self.orchestrator.client
        budget = state.request.options.revision_budget_per_stage
        issues: list[Issue] = []
        if trace:
            trace.event("stage_started", f"Revision: {step.name} step started.", stage=step.name)
        for attempt in range(budget + 1):
            context = state.context(
                include_sheets=step.sees_sheets, include_storyboard=step.sees_storyboard
            )
            context["revision"] = {
                "instruction": req.instruction,
                "target_ids": list(req.target_ids),
                "impacted_ids": sorted(scope.open_ids),
                "locked_ids": sorted(scope.locked),
                "current": _current(step.name, base),
            }
            gen = GenerationRequest(
                stage=step.name,
                instructions="\n\n".join(
                    p
                    for p in (
                        orch.base_prompt,
                        step.directive,
                        step.rules(state.request),
                        REVISE_RULES,
                        mode_rules(state.request, step.name),
                        OUTPUT_RULES,
                    )
                    if p
                ),
                context=context,
                feedback=issues,
                attempt=attempt,
                recorder=trace.recorder(step.name, attempt) if trace else NullRecorder(),
            )
            try:
                output = await client.generate(gen, step.output_model)
            finally:
                used = state.token_usage.setdefault(step.name, {})
                for k, v in gen.usage.items():
                    used[k] = used.get(k, 0) + v
            state.model_versions[step.name] = gen.model or client.model_id
            step.apply(state, output)
            restore(step.name, state, base, scope)
            if step.name == "storyboard":
                # The shots as restored. The model's design briefs count only where the
                # revision reaches; elsewhere the base revision's stay.
                finish_storyboard(state, output.style_brief,
                                  {o.requirement_id: o for o in output.design_overrides}
                                  | _kept_overrides(base, state, rev, scope))  # fmt: skip
            issues = step.check(state)
            if not issues:
                if trace:
                    trace.event("stage_passed", f"Revision: {step.name} passed.", stage=step.name)
                return []
            if trace:
                trace.event("gate_issues", f"Hard checks found {len(issues)} issues.",
                            stage=step.name, attempt=attempt,
                            issues=[f"{i.check} {i.object_id}: {i.evidence}" for i in issues[:10]])  # fmt: skip
        return issues

    def _needs_revision(
        self,
        rev: Revision,
        base: ProductionPackage,
        state: PipelineState,
        issues: list[Issue],
        *,
        step: str | None,
        trace: Tracer | None,
        pkg: ProductionPackage | None = None,
    ) -> Revision:
        """Keep the draft (unpublished) for inspection; the story stays on its base revision."""
        draft = pkg or state.draft_package()
        draft.experiment = base.experiment
        if self.repo.get_package(draft.package_id, draft.revision_id) is None:
            self.repo.save_package(draft, published=False)
        where = f"the {step} step" if step else "the final checks"
        self._end(rev, "needs_revision", changed_ids=changed_ids(base, draft),
                  issues=[f"{i.check} {i.object_id}: {i.evidence}" for i in issues],
                  error=f"{len(issues)} issues left at {where}")  # fmt: skip
        if trace:
            trace.event("revision_needs_revision",
                        f"Revision {rev.revision_id} not published: {rev.error}.", stage=step)  # fmt: skip
        return rev

    async def _wait_idle(self, package_id: str, revision_id: str) -> str | None:
        """Wait for the story's running work to finish; what is still busy after wait_s."""
        waited = 0.0
        while (busy := busy_work(self.repo, package_id, revision_id)) and waited < self.wait_s:
            await asyncio.sleep(self.poll_s)
            waited += self.poll_s
        return busy


def _state_from(base: ProductionPackage, revision_id: str) -> PipelineState:
    b = base.model_copy(deep=True)
    return PipelineState(
        request=b.provenance.request,
        package_id=b.package_id,
        revision_id=revision_id,
        source_brief=b.source_brief,
        story_bible=b.story_bible,
        episodes=b.episodes,
        cast_sheet=b.cast_sheet,
        set_sheet=b.set_sheet,
        prop_sheet=b.prop_sheet,
        shots=b.shot_list,
        timing=b.timing,
        asset_requirements=b.asset_requirements,
        qa_report=b.qa_report,
        model_versions=dict(b.provenance.model_versions),
        token_usage={},
    )


def _current(step: str, base: ProductionPackage) -> dict[str, Any] | None:
    """The step's current output where its context doesn't already show it."""
    dump = lambda x: x.model_dump(mode="json") if x is not None else None
    if step in _SHEET_FIELD:
        return {_SHEET_FIELD[step]: dump(getattr(base, _SHEET_FIELD[step]))}
    if step == "storyboard":
        return {
            "shots": [s.model_dump(mode="json") for s in base.shot_list],
            "timing": [t.model_dump(mode="json") for t in base.timing],
            "style_brief": dump(base.asset_requirements.style_brief),
            "asset_requirements": dump(base.asset_requirements),
        }
    return None
