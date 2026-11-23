"""An episode run: one button that takes an episode from its storyboard to its clips and
a rough assembly, with the plan and the progress in view and a person able to pause,
change course and continue.

The run is a loop over the episode's shots that reads the keyframe board as it is and
does the next thing each shot needs, with the same rules a person clicking would use
(flow.py): draw the design pictures if any are missing; plan a shot's cuts; draw each
cut's keyframe and approve the best draw (or wait for a person); write the video prompt
once every cut is approved; render the clip once the prompt is current and passes its
checks; and when every shot has its clip, cut the episode together (assemble.py). Nothing
is cached between steps, so whatever a person does meanwhile on the Keyframes tab (a new
plan, a different approval, a redraw, an edited prompt) is what the next step sees.

The run **holds** where a person is needed: a cut whose draws all failed twice, a prompt
that fails a check, a design pick that is missing, or wherever the settings say to wait
(`holds`: every approval, before the first render, before the assembly; `hold_shots`: the
shots whose keyframes a person wants to approve). A hold is released from the run's page
and the run goes on. **Pause** stops the loop; the keyframe work already started finishes
(like a story job's pause) unless the pause is `now`, which pauses the running takes and
stops the clip being rendered. **Resume** starts the loop again from the board as it is.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..design.prompts import plan_images
from ..media import MediaStore
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.package import utcnow
from ..models.storyboard import Shot
from ..videogen import VideoClient, VideoReference
from . import assemble as asm
from .approvals import approved_take, is_busy
from .board import picture_labels
from .clip_refs import VIDEO_MAX_REFERENCES, clip_references, exportable
from .clips import (
    ClipRef,
    ShotClip,
    clip_print,
    clip_prompt,
    clip_ratio,
    clip_seconds,
    clip_state,
    clip_view,
    latest_clip,
    next_seeds,
    queue_ahead,
    shot_clips,
)
from .common import NotReady, _names, shot_of
from .flow import approve_draw, start_prompt, start_take
from .models import DEFAULT_DRAWS, MAX_DRAWS, MAX_ROUNDS, ShotPlan, ShotTake, ShotVideoPrompt
from .picks import shot_picks
from .plans import current_sub, plan_of
from .takes import plan_take
from .video_prompt import approved_frames, fingerprint, prompt_state

# The fast option: a lower short edge and fewer reference pictures, to render sooner.
FAST_SHORT_EDGE = 512
FAST_MAX_REFERENCES = 3

log = logging.getLogger(__name__)

RunStatus = Literal["running", "paused", "done", "failed"]
HoldPoint = Literal["approve", "render", "assemble"]
MAX_EVENTS = 400
CUT_RETRIES = 2  # new takes for a cut whose take failed before the run holds
STEP_RETRIES = 1  # prompt writes and renders tried again before the run holds


class RunSettings(BaseModel):
    """How the run works: the keyframe settings, where it waits for a person, and how far
    it goes."""

    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)
    provider: str | None = Field(None, max_length=40)
    # Where the run waits for a person: "approve" (every keyframe is approved by hand),
    # "render" (before the first clip is rendered), "assemble" (before the episode is cut).
    holds: list[HoldPoint] = Field(default_factory=list)
    # Shots whose keyframes a person approves (the rest are approved by the run).
    hold_shots: list[str] = Field(default_factory=list)
    # Shots left out of the run and of the assembly.
    skip_shots: list[str] = Field(default_factory=list)
    # Approve the best draw of a cut when none passed the check (else hold there).
    approve_failed: bool = True
    render: bool = True
    assemble: bool = True
    # Shots whose keyframes are made at once (renders are one at a time anyway).
    parallel: int = Field(2, ge=1, le=6)
    # Render faster, rougher: a lower resolution and the cuts' keyframes as the only
    # reference pictures. Trades likeness and detail stability for speed.
    fast: bool = False


class RunEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    kind: Literal["info", "ok", "warn", "bad"] = "info"
    shot_id: str | None = None
    text: str


class EpisodeRun(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    package_id: str
    revision_id: str
    episode_id: str
    status: RunStatus = "running"
    settings: RunSettings = Field(default_factory=RunSettings)
    events: list[RunEvent] = Field(default_factory=list)
    # Where the run waits for a person now: key -> why. Keys: "<shot>:cut<i>",
    # "<shot>:prompt", "<shot>:clip", "<shot>:picks", "render", "assemble".
    holds: dict[str, str] = Field(default_factory=dict)
    # Holds a person released: the run doesn't hold there again. "<shot>:approve" releases
    # a shot's approvals, "approve" everyone's.
    released: list[str] = Field(default_factory=list)
    retries: dict[str, int] = Field(default_factory=dict)
    images_queued: bool = False
    output: dict[str, Any] | None = None  # the assembled episode: file, poster, duration_s
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def busy(self) -> bool:
        return self.status == "running"

    def event(self, text: str, kind: str = "info", shot_id: str | None = None) -> None:
        self.events.append(RunEvent(kind=kind, shot_id=shot_id, text=text))  # type: ignore[arg-type]
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]

    def freed(self, key: str) -> bool:
        return key in self.released


@dataclass
class ClipInputs:
    prompt: str
    sent: list[str]  # ref ids, in the order sent
    references: list[ClipRef]
    duration_s: int
    ratio: str
    print: str
    short_edge: int | None = None


def episode_shots(pkg: ProductionPackage, episode_id: str) -> list[Shot]:
    """The episode's shots in story order; KeyError for an unknown episode."""
    if not any(ep.episode_id == episode_id for ep in pkg.episodes):
        raise KeyError(episode_id)
    return sorted((s for s in pkg.shot_list if s.episode_id == episode_id),
                  key=lambda s: s.start_ms)  # fmt: skip


class EpisodeRunner:
    """Runs episode runs in the API process (one loop per run) and renders clips one at a
    time. Unfinished runs don't survive a restart as running: `recover` pauses them, and
    marks clips being rendered failed, for the person to resume."""

    def __init__(
        self,
        repo: Any,
        media: MediaStore,
        frames: Any,  # ShotFrameRunner
        video: VideoClient,
        *,
        poll_s: float = 2.0,
        image_wakeup: asyncio.Event | None = None,
    ) -> None:
        self.repo = repo
        self.media = media
        self.frames = frames
        self.video = video
        self.poll_s = poll_s
        self.image_wakeup = image_wakeup
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._renders: dict[str, asyncio.Task[None]] = {}
        self._render_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    def recover(self) -> int:
        n = self.repo.fail_unfinished_shot_clips(
            "Interrupted by a server restart. Render it again."
        )
        for run in self.repo.list_episode_runs(status="running"):
            run.status, run.error = "paused", "Paused by a server restart. Resume to go on."
            run.event(run.error, "warn")
            self._save(run)
            n += 1
        return n

    async def aclose(self) -> None:
        tasks = [*self._loops.values(), *self._renders.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _save(self, run: EpisodeRun) -> None:
        run.updated_at = utcnow()
        self.repo.save_episode_run(run)

    def start(self, run: EpisodeRun) -> EpisodeRun:
        """Save a new run and start its loop."""
        if any(r.busy for r in self.repo.list_episode_runs(
                run.package_id, run.revision_id, run.episode_id)):  # fmt: skip
            raise ValueError("this episode already has a run going; pause it first")
        run.status = "running"
        run.event("Run started.")
        self._save(run)
        self._spawn(run.run_id)
        return run

    def _spawn(self, run_id: str) -> None:
        old = self._loops.get(run_id)
        if old is not None and not old.done():
            return
        task = asyncio.create_task(self._loop(run_id))
        self._loops[run_id] = task
        task.add_done_callback(
            lambda t: self._loops.pop(run_id, None) if self._loops.get(run_id) is t else None
        )

    def poke(self, run_id: str) -> None:
        """Make the run's loop look at the board now."""
        self._wake.setdefault(run_id, asyncio.Event()).set()

    async def _loop(self, run_id: str) -> None:
        while True:
            run = self.repo.get_episode_run(run_id)
            if run is None or run.status != "running":
                return
            try:
                if await self.step(run):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("episode run %s failed", run_id, exc_info=True)
                run = self.repo.get_episode_run(run_id) or run
                run.status, run.error = "failed", str(exc)[:500]
                run.finished_at = utcnow()
                run.event(f"The run stopped: {run.error}", "bad")
                self._save(run)
                return
            wake = self._wake.setdefault(run_id, asyncio.Event())
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), self.poll_s)
            except TimeoutError:
                pass

    def _run_or_raise(self, run_id: str) -> EpisodeRun:
        run = self.repo.get_episode_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def pause(self, run_id: str, now: bool = False) -> EpisodeRun:
        """Stop the loop: the keyframe work already started finishes (its results are kept)
        unless `now`, which pauses the running takes and stops the clip being rendered."""
        run = self._run_or_raise(run_id)
        if run.status != "running":
            raise ValueError(f"only a running run can be paused, not one that is {run.status}")
        run.status = "paused"
        if now:
            shots = {s.shot_id for s in episode_shots_or_none(self.repo, run)}
            for t in self.repo.list_shot_takes(run.package_id, run.revision_id):
                if t.busy and t.shot_id in shots:
                    self.frames.pause(t.take_id)
            for clip in self.repo.list_shot_clips(run.package_id, run.revision_id):
                if (
                    clip.busy
                    and clip.run_id == run.run_id
                    and (task := self._renders.get(clip.clip_id))
                ):
                    task.cancel()
            run.event("Stopped now: running takes pause after their current step; the clip "
                      "being rendered is dropped.", "warn")  # fmt: skip
        else:
            run.event("Paused: what is already running finishes, nothing new starts.", "warn")
        self._save(run)
        return run

    def resume(self, run_id: str) -> EpisodeRun:
        run = self._run_or_raise(run_id)
        if run.status not in ("paused", "failed"):
            raise ValueError(
                f"only a paused or failed run can be resumed, not one that is {run.status}"
            )
        run.status, run.error, run.finished_at = "running", None, None
        run.event("Resumed.")
        self._save(run)
        self._spawn(run.run_id)
        return run

    def release(self, run_id: str, key: str) -> EpisodeRun:
        """Let the run go on past a hold: the key is remembered so it doesn't hold there
        again. "approve" / "render" / "assemble" also drop that hold point from the
        settings; "<shot>:cut<i>" forgets the cut's failed tries."""
        run = self._run_or_raise(run_id)
        if key in ("approve", "render", "assemble"):
            run.settings.holds = [h for h in run.settings.holds if h != key]
        if key.endswith(":approve"):
            shot_id = key.rsplit(":", 1)[0]
            run.settings.hold_shots = [s for s in run.settings.hold_shots if s != shot_id]
        if key not in run.released:
            run.released.append(key)
        run.retries.pop(key, None)
        run.holds.pop(key, None)
        run.event(f"Released: {key}.")
        self._save(run)
        self.poke(run_id)
        return run

    def retry_shot(self, run_id: str, shot_id: str) -> EpisodeRun:
        """Forget a shot's failed tries and holds, so the run tries it again."""
        run = self._run_or_raise(run_id)
        for key in [k for k in run.retries if k.startswith(f"{shot_id}:")]:
            del run.retries[key]
        for key in [k for k in run.holds if k.startswith(f"{shot_id}:")]:
            del run.holds[key]
        run.settings.skip_shots = [s for s in run.settings.skip_shots if s != shot_id]
        run.event("Trying the shot again.", shot_id=shot_id)
        self._save(run)
        self.poke(run_id)
        return run

    def update_settings(self, run_id: str, changes: dict[str, Any]) -> EpisodeRun:
        run = self._run_or_raise(run_id)
        run.settings = run.settings.model_copy(update=changes)
        RunSettings.model_validate(run.settings.model_dump())
        run.event("Settings changed: " + ", ".join(sorted(changes)) + ".")
        self._save(run)
        self.poke(run_id)
        return run

    # -- one step -------------------------------------------------------------

    def _options(self, settings: RunSettings) -> dict[str, Any]:
        return {"draws": settings.draws, "max_rounds": settings.max_rounds,
                **self.frames.take_options(settings.provider)}  # fmt: skip

    async def step(self, run: EpisodeRun) -> bool:
        """Do the next thing each shot needs. Returns whether the run is complete."""
        repo = self.repo
        pkg = repo.get_package(run.package_id, run.revision_id)
        if pkg is None:
            raise RuntimeError("the package revision is gone")
        s = run.settings
        shots = [x for x in episode_shots(pkg, run.episode_id) if x.shot_id not in s.skip_shots]
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = {p.shot_id: p for p in repo.list_shot_plans(pkg.package_id, pkg.revision_id)}
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        prompts = {p.shot_id: p for p in repo.list_video_prompts(pkg.package_id, pkg.revision_id)}
        clips = repo.list_shot_clips(pkg.package_id, pkg.revision_id)
        holds: dict[str, str] = {}
        active = sum(
            1 for p in plans.values() if p.busy and p.shot_id in {x.shot_id for x in shots}
        )
        rendering = sum(1 for c in clips if c.busy)
        complete = True

        # 0. The design pictures every shot draws from.
        missing = {x.shot_id: shot_picks(pkg, images, x).missing_for() for x in shots}
        if any(missing.values()):
            drawing = any(i.status in ("pending", "running") for i in images)
            if not drawing and not run.images_queued:
                planned = plan_images(pkg, images)
                for img in planned:
                    repo.save_image(img)
                run.images_queued = True
                if planned:
                    drawing = True
                    if self.image_wakeup is not None:
                        self.image_wakeup.set()
                    run.event(f"Drawing the design pictures first ({len(planned)} queued).")
            for shot_id, what in missing.items():
                if what:
                    holds[f"{shot_id}:picks"] = ("waiting for the design pictures" if drawing
                                                 else "first: " + "; ".join(what))  # fmt: skip
            complete = False

        for shot in shots:
            sid = shot.shot_id
            if missing.get(sid):
                continue
            stored = plans.get(sid)
            if stored is not None and stored.busy:
                complete = False
                continue
            plan = plan_of(pkg, shot, stored)
            if stored is None:
                complete = False
                if active >= s.parallel:
                    continue
                plan = ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                shot_id=sid, status="writing")  # fmt: skip
                repo.save_shot_plan(plan)
                self.frames.start_plan(plan, then={"sub_shot": 1, **self._options(s)})
                run.event("Planning the cuts; the first keyframe follows.", shot_id=sid)
                active += 1
                continue

            pending = next(
                (sub for sub in plan.sub_shots if not _approved(pkg, shot, plan, takes, sub.index)),
                None,
            )
            if pending is not None:
                complete = False
                i = pending.index
                key = f"{sid}:cut{i}"
                if is_busy(takes, sid, i):
                    active += 1
                    continue
                mine = [t for t in takes if t.shot_id == sid and t.sub_shot == i
                        and current_sub(pkg, shot, plan, t) is not None and not t.busy]  # fmt: skip
                newest = max(mine, key=lambda t: t.created_at, default=None)
                drawn = [d for d in newest.results if d.status == "done"] if newest else []
                if newest is not None and newest.status == "done" and drawn:
                    if (
                        ("approve" in s.holds and not run.freed("approve")) or sid in s.hold_shots
                    ) and not run.freed(f"{sid}:approve"):
                        holds[key] = f"cut {i} is drawn and waits for your approval"
                        continue
                    best = newest.best() or drawn[0]
                    if (
                        best.check is not None
                        and not best.check.passed
                        and not s.approve_failed
                        and not run.freed(key)
                    ):
                        why = best.check.summary or "no draw passed the check"
                        holds[key] = f"cut {i}: {why}"
                        continue
                    try:
                        approve_draw(repo, self.frames, pkg, newest, best.n, next_cut=True)
                    except ValueError as exc:
                        holds[key] = str(exc)
                        continue
                    takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
                    score = f" (score {best.check.score})" if best.check else ""
                    run.event(f"Cut {i}: approved draw {best.n + 1} of take round {newest.round}{score}.",
                              "ok", shot_id=sid)  # fmt: skip
                    active += 1
                    continue
                if newest is not None and newest.status == "paused":
                    holds[key] = f"cut {i}'s take is paused; resume it or approve a draw"
                    continue
                if active >= s.parallel:
                    continue
                if newest is not None:  # failed, or finished without a usable draw
                    tries = run.retries.get(key, 0)
                    if tries >= CUT_RETRIES and not run.freed(key):
                        holds[key] = (
                            f"cut {i} failed {tries} times: {newest.error or 'no draw came out'}"
                        )
                        continue
                    run.retries[key] = tries + 1
                    run.event(f"Cut {i} failed ({(newest.error or 'no draw')[:120]}); drawing it again.",
                              "warn", shot_id=sid)  # fmt: skip
                try:
                    take = plan_take(pkg, images, takes, plan, sub_shot=i, **self._options(s),
                                     reading=self.frames.fresh_reading(pkg, shot.scene_id))  # fmt: skip
                    start_take(repo, self.frames, take)
                except (NotReady, ValueError) as exc:
                    holds[key] = str(exc)
                    continue
                takes.append(take)
                if newest is None:
                    run.event(f"Drawing cut {i}'s keyframe.", shot_id=sid)
                active += 1
                continue

            # Every cut approved: the video prompt.
            vp = prompts.get(sid)
            context, ids = self.frames.video_inputs(pkg, shot)
            state = prompt_state(vp, fingerprint(context, ids))
            pkey = f"{sid}:prompt"
            if state == "writing":
                complete = False
                continue
            if state in ("none", "outdated"):
                complete = False
                start_prompt(repo, self.frames, pkg, sid)
                run.event("Writing the video prompt." if state == "none"
                          else "The video prompt is out of date; writing it again.", shot_id=sid)  # fmt: skip
                continue
            if state == "failed":
                complete = False
                tries = run.retries.get(pkey, 0)
                if tries >= STEP_RETRIES and not run.freed(pkey):
                    holds[pkey] = f"the video prompt couldn't be written: {vp.error if vp else ''}"
                    continue
                run.retries[pkey] = tries + 1
                start_prompt(repo, self.frames, pkg, sid)
                run.event("The video prompt failed; writing it again.", "warn", shot_id=sid)
                continue
            assert vp is not None
            if vp.problems and not run.freed(pkey):
                complete = False
                holds[pkey] = "the video prompt fails a check: " + "; ".join(vp.problems)
                continue
            if not s.render:
                continue

            # The clip.
            ckey = f"{sid}:clip"
            clip = latest_clip(clips, sid)
            inputs = self.clip_inputs(pkg, shot, plan, vp, takes, images, run.settings.fast)
            cs = clip_state(clip, inputs.print if inputs else None)
            if cs == "current":
                continue
            complete = False
            if cs == "rendering":
                continue
            if "render" in s.holds and not run.freed("render"):
                holds["render"] = "before the first clip is rendered"
                continue
            if cs == "failed":
                tries = run.retries.get(ckey, 0)
                if tries >= STEP_RETRIES and not run.freed(ckey):
                    holds[ckey] = f"the clip failed: {clip.error if clip else ''}"
                    continue
            if inputs is None:
                holds[ckey] = "the clip has no reference pictures to send"
                continue
            if rendering >= 1:
                continue  # one clip at a time
            if cs == "failed":
                run.retries[ckey] = run.retries.get(ckey, 0) + 1
                run.event("The clip failed; rendering it again.", "warn", shot_id=sid)
            else:
                run.event("Rendering the clip." if cs == "none"
                          else "The clip is out of date; rendering it again.", shot_id=sid)  # fmt: skip
            self.start_render(pkg, shot, run_id=run.run_id, fast=run.settings.fast)
            rendering += 1

        run.holds = holds
        if not complete:
            self._save(run)
            return False
        if s.render and s.assemble:
            clips = repo.list_shot_clips(pkg.package_id, pkg.revision_id)
            chosen = [c for x in shots if (c := latest_clip(clips, x.shot_id)) and c.file]
            print_ = "|".join(c.clip_id for c in chosen)
            if (run.output or {}).get("print") != print_:
                if "assemble" in s.holds and not run.freed("assemble"):
                    run.holds = {
                        "assemble": "every clip is rendered; before the episode is cut together"
                    }
                    self._save(run)
                    return False
                run.event(f"Cutting the episode together from {len(chosen)} clips.")
                self._save(run)
                out_rel = f"episodes/{pkg.package_id}/{run.run_id}.mp4"
                poster_rel = f"episodes/{pkg.package_id}/{run.run_id}.jpg"
                info = await asyncio.to_thread(
                    asm.assemble, [self.media.path(c.file) for c in chosen if c.file],
                    self.media.path(out_rel), self.media.path(poster_rel),
                )  # fmt: skip
                run.output = {"file": out_rel, "poster": poster_rel, "print": print_, **info}
        run.status, run.finished_at = "done", utcnow()
        run.event("Episode complete.", "ok")
        self._save(run)
        return True

    # -- clips --------------------------------------------------------------

    def clip_inputs(
        self,
        pkg: ProductionPackage,
        shot: Shot,
        plan: ShotPlan,
        vp: ShotVideoPrompt,
        takes: list[ShotTake],
        images: list[GeneratedImage],
        fast: bool = False,
    ) -> ClipInputs | None:
        """What the shot's clip is rendered from now: the prompt as H3 takes it, the
        pictures in the order sent (those with a file), its length and ratio, the short
        edge, and the print of all that. None when no picture can be sent. `fast` lowers
        the resolution and sends only the cuts' keyframes."""
        frames = approved_frames(pkg, shot, plan, takes)
        reading = self.frames.fresh_reading(pkg, shot.scene_id)
        sent, _, _ = clip_references(
            pkg,
            shot,
            plan,
            frames,
            reading,
            images,
            exportable(images).__contains__,
            limit=FAST_MAX_REFERENCES if fast else VIDEO_MAX_REFERENCES,
            keyframes_only=fast,
        )
        usable = [r for r in sent if (f := self.frames._ref_file(r["ref_id"])) and f[0].is_file()]
        if not usable:
            return None
        ids = [r["ref_id"] for r in usable]
        prompt = clip_prompt(vp, plan, ids)
        secs = clip_seconds(plan, shot.duration_ms)
        short_edge = FAST_SHORT_EDGE if fast else None
        refs = [ClipRef(ref_id=r["ref_id"], label=f"<Picture {i}>", kind=r["kind"], about=r["about"])
                for i, r in enumerate(usable, 1)]  # fmt: skip
        return ClipInputs(
            prompt,
            ids,
            refs,
            secs,
            clip_ratio(pkg),
            clip_print(vp, prompt, ids, secs, short_edge),
            short_edge,
        )

    def start_render(
        self,
        pkg: ProductionPackage,
        shot: Shot,
        run_id: str | None = None,
        fast: bool = False,
        seed: int | None = None,
    ) -> ShotClip:
        """A new clip of the shot, rendered in the background (one at a time). `fast`
        renders it at a lower resolution with keyframes-only references; `seed` is the
        take's seed (a seed the shot hasn't used when not given, so it is a new take)."""
        if seed is None:
            mine = shot_clips(self.repo.list_shot_clips(pkg.package_id, pkg.revision_id),
                              shot.shot_id)  # fmt: skip
            seed = next_seeds(mine, 1)[0]
        clip = ShotClip(package_id=pkg.package_id, revision_id=pkg.revision_id,
                        shot_id=shot.shot_id, run_id=run_id, fast=fast, seed=seed)  # fmt: skip
        self.repo.save_shot_clip(clip)
        task = asyncio.create_task(self._render(clip.clip_id))
        self._renders[clip.clip_id] = task
        task.add_done_callback(lambda _t: self._renders.pop(clip.clip_id, None))
        return clip

    async def _render(self, clip_id: str) -> None:
        clip = self.repo.get_shot_clip(clip_id)
        if clip is None:
            return
        try:
            async with self._render_lock:
                await self._render_clip(clip)
        except asyncio.CancelledError:
            clip.status, clip.error = "failed", "Stopped by the user."
            clip.finished_at = utcnow()
            self.repo.save_shot_clip(clip)
            raise
        except Exception as exc:
            log.warning("clip %s failed", clip_id, exc_info=True)
            clip.status, clip.error = "failed", str(exc)[:500]
            clip.finished_at = utcnow()
            self.repo.save_shot_clip(clip)
        if clip.run_id:
            self.poke(clip.run_id)

    async def _render_clip(self, clip: ShotClip) -> None:
        repo = self.repo
        pkg = repo.get_package(clip.package_id, clip.revision_id)
        if pkg is None:
            raise RuntimeError("the package revision is gone")
        shot = shot_of(pkg, clip.shot_id)
        plan = plan_of(pkg, shot, repo.get_shot_plan(pkg.package_id, pkg.revision_id, shot.shot_id))
        vp = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot.shot_id)
        if vp is None or vp.status != "done":
            raise RuntimeError("the shot has no finished video prompt")
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        inputs = self.clip_inputs(pkg, shot, plan, vp, takes, images, clip.fast)
        if inputs is None:
            raise RuntimeError("no reference picture can be sent with the clip")
        clip.status = "rendering"
        clip.prompt, clip.references = inputs.prompt, inputs.references
        clip.duration_s, clip.ratio, clip.written_from = (
            inputs.duration_s,
            inputs.ratio,
            inputs.print,
        )
        repo.save_shot_clip(clip)
        refs = await asyncio.to_thread(self._reference_bytes, inputs.sent)
        result = await self.video.generate(
            inputs.prompt, inputs.duration_s, inputs.ratio, refs, inputs.short_edge, clip.seed
        )
        rel = f"clips/{pkg.package_id}/{clip.clip_id}.mp4"
        poster = f"clips/{pkg.package_id}/{clip.clip_id}.jpg"
        await asyncio.to_thread(self._write_file, rel, result.data)
        try:
            await asyncio.to_thread(asm.poster_of, self.media.path(rel), self.media.path(poster))
        except asm.AssembleError:
            poster = None  # type: ignore[assignment]
        clip.file, clip.poster = rel, poster
        clip.content_type, clip.model, clip.task_id = (
            result.content_type,
            result.model,
            result.task_id,
        )
        clip.status, clip.error, clip.finished_at = "done", None, utcnow()
        repo.save_shot_clip(clip)

    def _reference_bytes(self, ids: list[str]) -> list[VideoReference]:
        out = []
        for ref_id in ids:
            found = self.frames._ref_file(ref_id)
            if found is None or not found[0].is_file():
                raise RuntimeError(f"reference picture {ref_id} is not available any more")
            path, mime = found
            out.append(VideoReference(path.name, path.read_bytes(), mime))
        return out

    def _write_file(self, rel: str, data: bytes) -> None:
        path = self.media.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # -- the view -------------------------------------------------------------

    def progress(self, run: EpisodeRun, pkg: ProductionPackage) -> dict[str, Any]:
        """The run with each shot's stage, the counts and a time estimate."""
        repo = self.repo
        s = run.settings
        shots = episode_shots(pkg, run.episode_id)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = {p.shot_id: p for p in repo.list_shot_plans(pkg.package_id, pkg.revision_id)}
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        prompts = {p.shot_id: p for p in repo.list_video_prompts(pkg.package_id, pkg.revision_id)}
        clips = repo.list_shot_clips(pkg.package_id, pkg.revision_id)
        names = _names(pkg)
        rows = []
        cuts = cuts_done = 0
        prompts_current = clips_done = 0
        for shot in shots:
            sid = shot.shot_id
            stored = plans.get(sid)
            plan = plan_of(pkg, shot, stored)
            approved = [_approved(pkg, shot, plan, takes, sub.index) for sub in plan.sub_shots]
            n, k = len(plan.sub_shots), sum(approved)
            holds = {key: why for key, why in run.holds.items() if key.startswith(f"{sid}:")}
            vp = prompts.get(sid)
            vstate = "none"
            labels: dict[str, str] = {}
            if k == n and n:
                context, ids = self.frames.video_inputs(pkg, shot)
                vstate = prompt_state(vp, fingerprint(context, ids))
                labels = picture_labels(context, ids)
            mine = shot_clips(clips, sid)
            clip = mine[0] if mine else None
            # A prompt that fails a check still renders once its hold is released.
            usable = vp is not None and (not vp.problems or run.freed(f"{sid}:prompt"))
            print_ = None  # what a clip would be rendered from now; None: not renderable
            if vstate == "current" and usable:
                inputs = self.clip_inputs(pkg, shot, plan, vp, takes, images, run.settings.fast)
                print_ = inputs.print if inputs else None
            cstate = clip_state(clip, print_)
            views = [clip_view(c, clip_state(c, print_)) for c in mine]
            for c, view in zip(mine, views, strict=True):
                if c.busy:
                    view["ahead"] = queue_ahead(clips, c)
            if sid in s.skip_shots:
                stage = "skipped"
            elif holds:
                stage = "waiting"
            elif stored is None or stored.busy:
                stage = "planning"
            elif k < n:
                stage = "keyframes"
            elif vstate != "current" or not usable:
                stage = "prompt"
            elif not s.render:
                stage = "ready"
            elif cstate == "current":
                stage = "done"
            else:
                stage = "rendering" if cstate == "rendering" else "render"
            if sid not in s.skip_shots:
                cuts += n
                cuts_done += k
                prompts_current += vstate == "current"
                clips_done += cstate == "current"
            rows.append({
                "shot_id": sid,
                "scene_id": shot.scene_id,
                "place": names.get(shot.location_id, shot.location_id),
                "people": [names.get(c, c) for c in shot.character_ids],
                "clip_ms": plan.sub_shots[-1].end_ms if plan.sub_shots else shot.duration_ms,
                "stage": stage,
                "cuts": n,
                "approved": k,
                "keyframes": [
                    {"index": sub.index, "approved": ok,
                     "take_id": (t := approved_take(takes, sid, sub.index)) and t.take_id,
                     "n": t.approved if t else None,
                     "busy": is_busy(takes, sid, sub.index)}
                    for sub, ok in zip(plan.sub_shots, approved, strict=True)
                ],
                "prompt": {"state": vstate, "problems": vp.problems if vp else [],
                           "error": vp.error if vp else None, "pictures": labels,
                           "blocks": [b.model_dump() for b in vp.blocks] if vp else [],
                           "sound": vp.sound if vp else "", "source": vp.source if vp else None},
                "clip": clip_view(clip, cstate),
                "clips": views,
                "holds": holds,
            })  # fmt: skip
        return {
            **run.model_dump(mode="json"),
            "shots": rows,
            "counts": {"shots": len([r for r in rows if r["stage"] != "skipped"]), "cuts": cuts,
                       "cuts_approved": cuts_done, "prompts_current": prompts_current,
                       "clips_done": clips_done, "holds": len(run.holds)},
            "estimate": estimate(run, shots, takes, clips, cuts - cuts_done,
                                 len(rows) - clips_done if s.render else 0),
        }  # fmt: skip


def _approved(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, takes: list[ShotTake], index: int
) -> bool:
    t = approved_take(takes, shot.shot_id, index)
    return t is not None and current_sub(pkg, shot, plan, t) is not None


def episode_shots_or_none(repo: Any, run: EpisodeRun) -> list[Shot]:
    pkg = repo.get_package(run.package_id, run.revision_id)
    if pkg is None:
        return []
    try:
        return episode_shots(pkg, run.episode_id)
    except KeyError:
        return []


def estimate(
    run: EpisodeRun,
    shots: list[Shot],
    takes: list[ShotTake],
    clips: list[ShotClip],
    cuts_left: int,
    clips_left: int,
) -> dict[str, Any]:
    """How long the rest may take, from what this episode's work took so far: the mean
    time of an approved keyframe's take (all its rounds) and of a finished clip. None
    where nothing has finished yet to measure by."""
    ids = {s.shot_id for s in shots}
    cut_s = [(t.finished_at - t.created_at).total_seconds() for t in takes
             if t.shot_id in ids and t.approved is not None and t.finished_at]  # fmt: skip
    clip_s = [(c.finished_at - c.created_at).total_seconds() for c in clips
              if c.shot_id in ids and c.status == "done" and c.finished_at]  # fmt: skip
    per_cut = sum(cut_s) / len(cut_s) if cut_s else None
    per_clip = sum(clip_s) / len(clip_s) if clip_s else None
    remaining = None
    if (cuts_left == 0 or per_cut is not None) and (clips_left == 0 or per_clip is not None):
        remaining = cuts_left * (per_cut or 0) + clips_left * (per_clip or 0)
    end = run.finished_at or utcnow()
    return {"elapsed_s": (end - run.created_at).total_seconds(), "per_cut_s": per_cut,
            "per_clip_s": per_clip, "cuts_left": cuts_left, "clips_left": clips_left,
            "remaining_s": remaining}  # fmt: skip
