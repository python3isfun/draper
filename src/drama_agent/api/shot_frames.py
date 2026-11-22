"""HTTP endpoints of the shot keyframes (see drama_agent.shot_frames)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from ..content_guard import GuardRefused, screen
from ..design.setup import make_thumb
from ..images import PICTURE_OUTDATED, story_rating
from ..media import MediaStore
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.package import utcnow
from ..models.storyboard import Shot
from ..pace import Pace, pace_name, story_pace
from ..shot_frames import (
    DEFAULT_DRAWS,
    KF_BEFORE_OUTDATED,
    MAX_CLIP_S,
    MAX_DRAWS,
    MAX_ROUNDS,
    MAX_SUB_SHOTS,
    MIN_CLIP_S,
    FrameDraw,
    NotReady,
    PlanProblems,
    PromptVersion,
    ShotFrameRunner,
    ShotPlan,
    ShotPlanVersion,
    ShotTake,
    approved_take,
    current_sub,
    design_print,
    frame_ref,
    frame_size,
    intimate_scene,
    is_busy,
    is_hard,
    make_sub_shots,
    outdated_keyframes,
    plan_of,
    plan_take,
    previous_shot_frame,
    prompt_findings,
    shot_board,
    shot_of,
    shot_picks,
    sub_shot_for,
)
from ..shot_reading import SceneReading, candidates, scene_shots
from ..shots.history import replace_plan, restore_plan
from ..shots.lock import KeyframeReading, SeenCut, lock_shot, unlock_shot
from ..shots.models import ShotVideoPrompt
from ..shots.references import sub_shot_references
from ..shots.staleness import keyframe_judge
from ..shots.video_prompt import approved_frames, fingerprint, prompt_state
from .schemas import UseKeyframeRequest


class KeyframeRequest(BaseModel):
    revision_id: str | None = None
    sub_shot: int = Field(1, ge=1, le=MAX_SUB_SHOTS)
    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    seeds: list[int] = Field(default_factory=list, max_length=MAX_DRAWS)
    direction: str | None = Field(None, max_length=1000)
    # Draw a saved prompt version as it is, or `prompt` (saved as a new version first).
    prompt_version: int | None = Field(None, ge=1)
    prompt: str | None = None
    based_on: int | None = Field(None, ge=1)  # with `prompt`: the version it edits
    note: str = Field("", max_length=500)  # with `prompt`: what was changed
    auto: bool = True
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)
    # The image provider ("h3" or "nano-banana"); None: the default (the board lists them).
    provider: str | None = Field(None, max_length=40)


class PromptRequest(BaseModel):
    revision_id: str | None = None
    sub_shot: int = Field(1, ge=1, le=MAX_SUB_SHOTS)
    prompt: str = Field(min_length=1)
    based_on: int | None = Field(None, ge=1)  # the version this edits
    note: str = Field("", max_length=500)


class KeyframeBatchRequest(BaseModel):
    revision_id: str | None = None
    # None: every shot without an approved first keyframe and with nothing running.
    shot_ids: list[str] | None = None
    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)
    # Plan the sub-shots of shots that have no plan first (the text model), then draw.
    plan: bool = True
    provider: str | None = Field(None, max_length=40)
    # Redraw out-of-date keyframes instead: per shot, the first cut whose approved keyframe
    # is out of date (the later ones follow as each is approved).
    outdated: bool = False


class SubShotIn(BaseModel):
    start_ms: int = Field(ge=0)
    framing: str = Field(max_length=1000)
    camera: str = Field("", max_length=1000)
    action: str = Field(max_length=2000)
    people: list[str] = Field(default_factory=list)  # character ids on screen
    # Character id -> where they are in the frame / which side they are off it.
    placement: dict[str, Literal["left", "center", "right"]] = Field(default_factory=dict)
    off_screen: dict[str, Literal["left", "right"]] = Field(default_factory=dict)


class PlanRequest(BaseModel):
    revision_id: str | None = None
    # Your own sub-shots; None asks the text model to plan them.
    sub_shots: list[SubShotIn] | None = Field(None, max_length=MAX_SUB_SHOTS)
    # The clip's length in whole seconds (null: the storyboard's, rounded up; left out:
    # what the shot's plan has now).
    clip_s: int | None = Field(None, ge=MIN_CLIP_S, le=MAX_CLIP_S)
    # The shot's own pace (null: the story's) and how many sub-shots to cut it into (null:
    # the planner decides). Left out: what the shot's plan has now.
    pace: Pace | None = None
    cuts: int | None = Field(None, ge=1, le=MAX_SUB_SHOTS)

    @field_validator("pace", mode="before")
    @classmethod
    def _pace(cls, value: object) -> object:
        return pace_name(value) or None if isinstance(value, str) else value


class InsertRequest(BaseModel):
    revision_id: str | None = None
    # The cut the new one follows (1 = between cuts 1 and 2; the last cut: at the end).
    after: int = Field(ge=1, le=MAX_SUB_SHOTS - 1)
    # What the new cut shows, in the director's words; the text model writes the cut.
    note: str = Field(min_length=1, max_length=1000)
    # Draw the new cut's keyframe once it is planned (when the cut before is approved).
    draw: bool = True
    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)
    provider: str | None = None


class ReadingRequest(BaseModel):
    revision_id: str | None = None


class RevisionBody(BaseModel):
    revision_id: str | None = None


class RestoredPlan(BaseModel):
    plan: ShotPlan
    # The old plan's keyframes approved again (their take and draw still exist).
    keyframes_approved: int


class ChoiceRequest(BaseModel):
    revision_id: str | None = None
    sub_shot: int = Field(1, ge=1, le=MAX_SUB_SHOTS)
    character_id: str
    image_id: str


class ApproveRequest(BaseModel):
    n: int = Field(ge=0)
    # Start the next sub-shot's keyframe, drawn from this one, with the same settings
    # (unless it already has a take drawn from this one).
    next: bool = True


class LockRequest(BaseModel):
    revision_id: str | None = None
    # The keyframes' reading as the editor confirmed it, one per cut, in order.
    cuts: list[SeenCut]


class VideoPromptsRequest(BaseModel):
    revision_id: str | None = None
    # Only these shots (None: every shot whose prompt is missing, failed or out of date).
    shot_ids: list[str] | None = None


class KeyframeBoard(BaseModel):
    package_id: str
    revision_id: str
    image_model: str
    # The providers a keyframe can be drawn with (name -> model), and the default.
    providers: dict[str, str] = Field(default_factory=dict)
    default_provider: str = "h3"
    busy: int
    shots: list[dict[str, Any]]


def register(
    app: FastAPI,
    *,
    repo: Any,
    media: MediaStore,
    runner: ShotFrameRunner,
    text_client: Any,
    image_model: str,
    package_or_404: Callable[[str, str | None], ProductionPackage],
    package_job: Callable[..., Any],
    file_response: Callable[[str, str], FileResponse],
    experiments: Any = None,
) -> None:
    def _board(pkg: ProductionPackage) -> KeyframeBoard:
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = repo.list_shot_plans(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        versions = repo.list_prompt_versions(pkg.package_id, pkg.revision_id)
        readings = repo.list_scene_readings(pkg.package_id, pkg.revision_id)
        prompts = repo.list_video_prompts(pkg.package_id, pkg.revision_id)
        lock_readings = repo.list_keyframe_readings(pkg.package_id, pkg.revision_id)
        shots = shot_board(pkg, images, takes, plans, versions, readings,
                           story_pace(repo, pkg.package_id), prompts, lock_readings)  # fmt: skip
        history = repo.count_plan_versions(pkg.package_id, pkg.revision_id)
        for shot in shots:  # a take asked to pause says so until it does
            shot["plan"]["history"] = history.get(shot["shot_id"], 0)  # earlier plans
            for sub in shot["sub_shots"]:
                for t in sub["takes"]:
                    t["pausing"] = t["take_id"] in runner.pausing
        return KeyframeBoard(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            image_model=image_model,
            busy=sum(1 for t in takes if t.busy)
            + sum(1 for p in plans if p.busy)
            + sum(1 for r in readings if r.busy)
            + sum(1 for p in prompts if p.busy)
            + sum(1 for r in lock_readings if r.busy),
            shots=shots,
            providers={name: c.model_id for name, c in runner.providers.items()},
            default_provider=runner.default_provider,
        )

    def _start_prompt(pkg: ProductionPackage, shot_id: str) -> ShotVideoPrompt:
        """Write the shot's video prompt in the background; the prompt it replaces stays
        shown meanwhile."""
        stored = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot_id)
        vp = (stored.model_copy(update={"status": "writing", "error": None}) if stored
              else ShotVideoPrompt(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                   shot_id=shot_id))  # fmt: skip
        repo.save_video_prompt(vp)
        runner.start_video_prompt(vp)
        return vp

    def _auto_prompt(pkg: ProductionPackage, shot: Shot) -> None:
        """Once every cut of the shot has its approved keyframe, write its video prompt,
        unless the one it has is current or being written."""
        stored = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot.shot_id)
        if stored is not None and stored.busy:
            return
        _, plan = _plan(pkg, shot)
        frames = approved_frames(pkg, shot, plan,
                                 repo.list_shot_takes(pkg.package_id, pkg.revision_id))  # fmt: skip
        if not frames or not all(frames.values()):
            return
        context, ids = runner.video_inputs(pkg, shot)
        if prompt_state(stored, fingerprint(context, ids)) != "current":
            _start_prompt(pkg, shot.shot_id)

    def _unlocked(stored: ShotPlan | None, shot_id: str) -> None:
        if stored is not None and stored.source == "keyframes":
            raise HTTPException(409, f"{shot_id} is locked to its keyframes; unlock it to change "
                                "the plan")  # fmt: skip

    def _take_or_404(take_id: str) -> ShotTake:
        take = repo.get_shot_take(take_id)
        if take is None:
            raise HTTPException(404, "take not found")
        return take

    def _writable(package_id: str, revision_id: str | None) -> ProductionPackage:
        pkg = package_or_404(package_id, revision_id)
        package_job(pkg.package_id, None, pkg.revision_id)  # refuses archived stories
        return pkg

    def _shot(pkg: ProductionPackage, shot_id: str) -> Shot:
        try:
            return shot_of(pkg, shot_id)
        except KeyError:
            raise HTTPException(404, f"no shot {shot_id}") from None

    def _plan(pkg: ProductionPackage, shot: Shot) -> tuple[ShotPlan | None, ShotPlan]:
        """(the stored plan, if any; the plan in force)."""
        stored = repo.get_shot_plan(pkg.package_id, pkg.revision_id, shot.shot_id)
        return stored, plan_of(pkg, shot, stored)

    def _reading(pkg: ProductionPackage, shot: Shot) -> SceneReading | None:
        """The shot's scene reading when it read the scene as it is now (else the runner
        reads it before the take's prompt is written)."""
        return runner.fresh_reading(pkg, shot.scene_id)

    def _start(take: ShotTake) -> ShotTake:
        stored = repo.get_shot_plan(take.package_id, take.revision_id, take.shot_id)
        if stored is not None and stored.busy:  # an insert moves its takes when written
            raise HTTPException(409, f"{take.shot_id} is being planned; draw once it is done")
        repo.save_shot_take(take)
        runner.start(take.take_id)
        return take

    @app.get("/v1/drama-packages/{package_id}/keyframes", response_model=KeyframeBoard)
    async def keyframe_board(package_id: str, revision_id: str | None = None) -> KeyframeBoard:
        """Every shot with its sub-shot plan, and per sub-shot its reference pictures,
        what is missing, its keyframe takes (each draw with its quality check) and its
        approved keyframe."""
        return _board(package_or_404(package_id, revision_id))

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/scenes/{scene_id}/reading",
        status_code=202,
    )
    async def read_scene(package_id: str, scene_id: str, req: ReadingRequest) -> dict[str, Any]:
        """Read the scene again (in the background): each person's state in every sub-shot,
        and the design picture closest to it. Your own picture choices carry over."""
        pkg = _writable(package_id, req.revision_id)
        if not any(s.scene_id == scene_id for s in pkg.shot_list):
            raise HTTPException(404, f"no scene {scene_id}")
        stored = repo.get_scene_reading(pkg.package_id, pkg.revision_id, scene_id)
        if stored is not None and stored.busy:
            raise HTTPException(409, "the scene is being read")
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        people = sorted({c for s in scene_shots(pkg, scene_id) for c in s.character_ids})
        if not any(candidates(pkg, images, who) for who in people):
            raise HTTPException(409, "pick design pictures for the scene's people first")
        # Marked as being read before this returns, so a second click is refused.
        busy = (stored or SceneReading(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                       scene_id=scene_id)).model_copy(update={"status": "writing"})  # fmt: skip
        repo.save_scene_reading(busy)
        runner.start_reading(pkg, scene_id)
        return {"scene_id": scene_id, "status": "writing"}

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/choice",
        response_model=SceneReading,
    )
    async def choose_picture(package_id: str, shot_id: str, req: ChoiceRequest) -> SceneReading:
        """Draw a person in one sub-shot from another of their design pictures than the one
        the reading chose. Their state stays the reading's; the prompt says what differs."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored = repo.get_scene_reading(pkg.package_id, pkg.revision_id, shot.scene_id)
        if stored is not None and stored.busy:
            raise HTTPException(409, "the scene is being read; choose when it is done")
        reading = _reading(pkg, shot)
        if reading is None:
            raise HTTPException(409, "read the scene first: its reading is missing or out of date")
        choice = reading.choice(shot_id, req.sub_shot, req.character_id)
        if choice is None:
            raise HTTPException(
                404, f"{req.character_id} is not on screen in sub-shot {req.sub_shot}"
            )
        if not any(c.image_id == req.image_id and c.character_id == req.character_id
                   for c in reading.candidates):  # fmt: skip
            raise HTTPException(422, f"{req.image_id} is not one of {req.character_id}'s pictures")
        choice.image_id, choice.keep = req.image_id, []
        choice.source, choice.reason = "user", "Chosen by you."
        repo.save_scene_reading(reading)
        return reading

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/plan",
        status_code=202,
        response_model=ShotPlan,
    )
    async def plan_sub_shots(package_id: str, shot_id: str, req: PlanRequest) -> ShotPlan:
        """Set a shot's sub-shots: your own (`sub_shots`, checked), or planned by the text
        model in the background. Approved keyframes the new plan no longer matches are
        withdrawn, with the later sub-shots drawn from them."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, _ = _plan(pkg, shot)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if (stored is not None and stored.busy) or is_busy(takes, shot_id):
            raise HTTPException(409, f"{shot_id} is still being planned or drawn")
        _unlocked(stored, shot_id)
        plan = ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id, shot_id=shot_id)
        # The shot's clip, pace and cut count: as asked (null: the storyboard's clip, the
        # story's pace, the planner's count), else as its plan has them.
        given = req.model_fields_set
        clip_ms = (req.clip_s * 1000 if req.clip_s else None) if "clip_s" in given else (
            stored.clip_ms if stored else None)  # fmt: skip
        pace = req.pace if "pace" in given else (stored.pace if stored else None)
        cuts = req.cuts if "cuts" in given else (stored.cuts if stored else None)
        if req.sub_shots is None:
            if stored is not None:
                # The board keeps showing the old plan meanwhile, whole (its clip, notes,
                # model, settings), so the history keeps it as it was when replaced. The
                # runner puts the asked settings on it, written or not.
                plan = stored.model_copy(update={"error": None})
            plan.status = "writing"
            repo.save_shot_plan(plan)
            runner.start_plan(plan, clip_ms=clip_ms, pace=pace, cuts=cuts)
            return plan.model_copy(update={"pace": pace, "cuts": cuts})
        if cuts is not None and cuts != len(req.sub_shots):
            if "cuts" in given:
                raise HTTPException(
                    422, f"{len(req.sub_shots)} sub-shots given, but cuts asks for {cuts}"
                )
            cuts = None  # your own cuts replace the count asked of the planner
        plan.clip_ms, plan.pace, plan.cuts = clip_ms, pace, cuts
        try:
            plan.sub_shots = make_sub_shots(
                pkg,
                shot,
                [s.model_dump() for s in req.sub_shots],
                plan.clip_ms,
            )
        except PlanProblems as exc:
            raise HTTPException(
                422, "Not a clean spec for video: " + "; ".join(exc.problems) + "."
            ) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        plan.source, plan.updated_at = "user", utcnow()
        plan.design_print = design_print(pkg, shot)
        replace_plan(repo, pkg, shot, plan, "user")  # withdraws approvals it doesn't match
        return plan

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/insert",
        status_code=202,
        response_model=ShotPlan,
    )
    async def insert_cut(package_id: str, shot_id: str, req: InsertRequest) -> ShotPlan:
        """Insert one cut after cut `after`, from the director's note: the text model writes
        it and re-times the shot in the background, keeping every other cut's view. Their
        keyframes stay approved, moved to their new numbers; the cut right after the new one
        is out of date until it is redrawn from the new cut's keyframe. With `draw`, the new
        cut's keyframe is drawn once it is planned."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, now = _plan(pkg, shot)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if (stored is not None and stored.busy) or is_busy(takes, shot_id):
            raise HTTPException(409, f"{shot_id} is still being planned or drawn")
        _unlocked(stored, shot_id)
        n = len(now.sub_shots)
        if n >= MAX_SUB_SHOTS:
            raise HTTPException(422, f"{shot_id} already has {n} cuts, the most a shot holds")
        if req.after > n:
            raise HTTPException(
                422, f"{shot_id} has {n} cut{'s' if n > 1 else ''}, no cut {req.after}"
            )
        then = None
        if req.draw:
            try:
                then = {"sub_shot": req.after + 1, "draws": req.draws,
                        "max_rounds": req.max_rounds, **runner.take_options(req.provider)}  # fmt: skip
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        # The plan in force (the storyboard's single cut, if none is stored) is what the board
        # keeps showing while the new one is written.
        plan = now.model_copy(update={"status": "writing", "error": None}, deep=True)
        repo.save_shot_plan(plan)
        runner.start_plan(plan, then=then, clip_ms=now.clip_ms, pace=now.pace,
                          insert=(req.after, req.note.strip()))  # fmt: skip
        return plan

    @app.get(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/plans",
        response_model=list[ShotPlanVersion],
    )
    async def plan_history(
        package_id: str, shot_id: str, revision_id: str | None = None
    ) -> list[ShotPlanVersion]:
        """A shot's earlier cut plans, newest first, each with the keyframes approved for
        it when it was replaced."""
        pkg = package_or_404(package_id, revision_id)
        _shot(pkg, shot_id)
        return list(reversed(repo.list_plan_versions(pkg.package_id, pkg.revision_id, shot_id)))

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/plans/{version}/restore",
        response_model=RestoredPlan,
    )
    async def restore_shot_plan(
        package_id: str, shot_id: str, version: int, req: RevisionBody
    ) -> RestoredPlan:
        """Make an earlier cut plan the shot's plan again. The plan in force goes to the
        history; the old plan's keyframes come back, approved again where their draw still
        exists, and approvals the restored plan doesn't match are withdrawn."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, _ = _plan(pkg, shot)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if (stored is not None and stored.busy) or is_busy(takes, shot_id):
            raise HTTPException(409, f"{shot_id} is still being planned or drawn")
        try:
            plan, back = restore_plan(repo, pkg, shot, version)
        except KeyError:
            raise HTTPException(404, f"{shot_id} has no plan version {version}") from None
        except ValueError as exc:
            raise HTTPException(422, f"That plan no longer fits the shot: {exc}") from None
        _auto_prompt(pkg, shot)
        return RestoredPlan(plan=plan, keyframes_approved=back)

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/read-keyframes",
        status_code=202,
        response_model=KeyframeReading,
    )
    async def read_keyframes(package_id: str, shot_id: str, req: RevisionBody) -> KeyframeReading:
        """Read the shot's approved keyframes (the vision model, in the background): what
        each cut shows, in the plan's words, for the editor to correct and lock the shot
        with (shots/lock.py). Every cut needs an approved keyframe."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, plan = _plan(pkg, shot)
        if stored is not None and stored.busy:
            raise HTTPException(409, f"{shot_id} is being planned")
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        frames = approved_frames(pkg, shot, plan, takes)
        if not frames or not all(frames.values()):
            raise HTTPException(409, f"approve a keyframe for every cut of {shot_id} first")
        reading = repo.get_keyframe_reading(pkg.package_id, pkg.revision_id, shot_id)
        if reading is not None and reading.busy:
            raise HTTPException(409, f"{shot_id}'s keyframes are being read")
        reading = KeyframeReading(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                  shot_id=shot_id)  # fmt: skip
        repo.save_keyframe_reading(reading)
        runner.start_keyframe_reading(reading)
        return reading

    @app.post("/v1/drama-packages/{package_id}/keyframes/{shot_id}/lock", response_model=ShotPlan)
    async def lock_keyframes(package_id: str, shot_id: str, req: LockRequest) -> ShotPlan:
        """Lock the shot: the reading of its keyframes (as the editor confirmed it, one entry
        per cut) becomes its plan's view; the draft plan goes to the history; the keyframes
        stay approved; the video prompt is written again from it."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, plan = _plan(pkg, shot)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if (stored is not None and stored.busy) or is_busy(takes, shot_id):
            raise HTTPException(409, f"{shot_id} is still being planned or drawn")
        frames = approved_frames(pkg, shot, plan, takes)
        if not frames or not all(frames.values()):
            raise HTTPException(409, f"approve a keyframe for every cut of {shot_id} first")
        try:
            new = lock_shot(repo, pkg, shot, plan, req.cuts)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        _auto_prompt(pkg, shot)
        return new

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/unlock", response_model=RestoredPlan
    )
    async def unlock_keyframes(package_id: str, shot_id: str, req: RevisionBody) -> RestoredPlan:
        """Unlock the shot: the draft plan the lock replaced is restored (its keyframes stay
        approved), so cuts can be planned, edited and inserted again."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored, _ = _plan(pkg, shot)
        if stored is None or stored.source != "keyframes":
            raise HTTPException(409, f"{shot_id} isn't locked")
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if is_busy(takes, shot_id):
            raise HTTPException(409, f"{shot_id} is still being drawn")
        try:
            plan, back = unlock_shot(repo, pkg, shot, stored)
        except KeyError:
            raise HTTPException(
                409, f"{shot_id}'s draft plan is no longer in its history"
            ) from None
        except ValueError as exc:
            raise HTTPException(422, f"That plan no longer fits the shot: {exc}") from None
        return RestoredPlan(plan=plan, keyframes_approved=back)

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/video-prompt",
        status_code=202,
        response_model=ShotVideoPrompt,
    )
    async def write_video_prompt(
        package_id: str, shot_id: str, req: RevisionBody
    ) -> ShotVideoPrompt:
        """Write (or write again) the shot's H3 video prompt from its plan, keyframes and
        scene reading as they are now (shots/video_prompt.py), in the background. It is
        written on its own once every cut has an approved keyframe; this writes it sooner,
        or again."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        stored = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot_id)
        if stored is not None and stored.busy:
            raise HTTPException(409, f"{shot_id}'s video prompt is being written")
        stored_plan, _ = _plan(pkg, shot)
        if stored_plan is not None and stored_plan.busy:
            raise HTTPException(
                409, f"{shot_id} is being planned; write its prompt once it is done"
            )
        return _start_prompt(pkg, shot_id)

    @app.post("/v1/drama-packages/{package_id}/keyframes/video-prompts", status_code=202)
    async def write_video_prompts(package_id: str, req: VideoPromptsRequest) -> dict[str, Any]:
        """Write the video prompt of every shot (or of `shot_ids`) whose prompt is missing,
        failed or out of date; shots being planned or written are skipped."""
        pkg = _writable(package_id, req.revision_id)
        board = {s["shot_id"]: s for s in _board(pkg).shots}
        if req.shot_ids is not None and (unknown := set(req.shot_ids) - set(board)):
            raise HTTPException(404, f"no shot {', '.join(sorted(unknown))}")
        started, skipped = [], []
        for shot_id, s in board.items():
            if req.shot_ids is not None and shot_id not in req.shot_ids:
                continue
            if s["video_prompt"]["state"] == "current" and req.shot_ids is None:
                continue
            if s["video_prompt"]["state"] == "writing" or s["plan"]["status"] == "writing":
                skipped.append(shot_id)
                continue
            _start_prompt(pkg, shot_id)
            started.append(shot_id)
        return {"started": started, "skipped": skipped}

    async def _save_version(
        pkg: ProductionPackage,
        shot: Shot,
        plan: ShotPlan,
        req: PromptRequest,
        reading: SceneReading | None = None,
    ) -> PromptVersion:
        """A person's prompt as the sub-shot's next version: checked against the H3 image
        rules (a picture it can't cite, empty or too long is refused; other findings are
        kept on the version) and screened by the content guard."""
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        try:
            sub, refs = sub_shot_for(pkg, images, takes, plan, req.sub_shot, reading)
        except NotReady as exc:
            raise HTTPException(409, str(exc)) from None
        prompt = req.prompt.strip()
        rating = story_rating(repo, pkg.package_id)
        findings = prompt_findings(prompt, refs, rating=rating)
        if hard := [f for f in findings if is_hard(f)]:
            raise HTTPException(422, "The prompt " + "; ".join(hard) + ".")
        if (level := intimate_scene(pkg, shot)) and any(r.likeness for r in refs):
            # The runner's rule, said before saving: no intimacy drawn from a real likeness.
            raise HTTPException(422, f"this shot is in a scene staged as {level} intimacy, and a "
                                "picked design image shows a real person's likeness (content "
                                "rule 3): pick a design image without a likeness")  # fmt: skip
        try:
            # Open-mode rules: the face and look pictures are the production's chosen
            # designs. Each says whether it shows a real person's likeness (its recorded
            # flag), so the screen doesn't have to guess: a designed character in a bedroom
            # scene is not rule 3's case.
            await screen(
                text_client,
                prompt,
                pictures=[
                    {"picture": f"<Picture {n}>", "kind": r.role + (
                        "; shows a real person's likeness" if r.likeness else
                        "; a designed character, set or prop made by this production (no real "
                        "person's likeness)")}
                    for n, r in enumerate(refs, 1)
                ],
                restrictive=False,
                rating=rating,
            )  # fmt: skip
        except GuardRefused as exc:
            raise HTTPException(422, str(exc)) from exc
        return repo.add_prompt_version(
            PromptVersion(
                package_id=pkg.package_id, revision_id=pkg.revision_id, shot_id=shot.shot_id,
                sub_shot=req.sub_shot, spec=sub, prompt=prompt, source="user",
                note=req.note.strip(), based_on=req.based_on, findings=findings,
                references=refs,
            )
        )  # fmt: skip

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}/prompts",
        status_code=201,
        response_model=PromptVersion,
    )
    async def save_prompt_version(
        package_id: str, shot_id: str, req: PromptRequest
    ) -> PromptVersion:
        """Save an edited prompt as the sub-shot's next version (nothing is drawn). Restore
        an old version by saving its text again."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        _, plan = _plan(pkg, shot)
        return await _save_version(pkg, shot, plan, req, _reading(pkg, shot))

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}",
        status_code=202,
        response_model=ShotTake,
    )
    async def new_keyframe_take(package_id: str, shot_id: str, req: KeyframeRequest) -> ShotTake:
        """A new take on a sub-shot's keyframe: written by the model (steered by
        `direction`, revising the newest take against its check), or a prompt version
        drawn as it is (`prompt_version`, or `prompt`, saved as a new version first) with
        the newest take's seeds unless `seeds` are given. Drawn, checked and, for a written
        prompt with `auto`, revised until a draw passes."""
        pkg = _writable(package_id, req.revision_id)
        shot = _shot(pkg, shot_id)
        _, plan = _plan(pkg, shot)
        version = None
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        reading = _reading(pkg, shot)  # once per request
        try:
            options = runner.take_options(req.provider)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if req.prompt is not None:
            # Check the draw can start before the prompt is saved, so a refused draw
            # leaves no version behind.
            try:
                plan_take(pkg, images, takes, plan, sub_shot=req.sub_shot, draws=req.draws,
                          reading=reading, **options)  # fmt: skip
            except NotReady as exc:
                raise HTTPException(409, str(exc)) from None
            version = await _save_version(
                pkg, shot, plan,
                PromptRequest(revision_id=req.revision_id, sub_shot=req.sub_shot,
                              prompt=req.prompt, based_on=req.based_on, note=req.note),
                reading,
            )  # fmt: skip
        elif req.prompt_version is not None:
            version = next(
                (v for v in repo.list_prompt_versions(pkg.package_id, pkg.revision_id)
                 if (v.shot_id, v.sub_shot, v.v) == (shot_id, req.sub_shot, req.prompt_version)),
                None,
            )  # fmt: skip
            if version is None:
                raise HTTPException(404, f"no prompt v{req.prompt_version} for this sub-shot")
        try:
            take = plan_take(
                pkg,
                images,
                takes,
                plan,
                sub_shot=req.sub_shot,
                draws=req.draws,
                seeds=req.seeds or None,
                direction=req.direction,
                version=version,
                auto=req.auto,
                max_rounds=req.max_rounds,
                reading=reading,
                **options,
            )
        except NotReady as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return _start(take)

    @app.post("/v1/drama-packages/{package_id}/keyframes", status_code=202)
    async def keyframe_batch(package_id: str, req: KeyframeBatchRequest) -> dict[str, Any]:
        """First keyframes for many shots at once (default: every shot without an approved
        first keyframe and with nothing running). With `plan`, a shot with no sub-shot
        plan is planned first and drawn once planned. Later sub-shots start as the one
        before is approved. Shots that can't start yet are listed with the reason."""
        pkg = _writable(package_id, req.revision_id)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = {p.shot_id: p for p in repo.list_shot_plans(pkg.package_id, pkg.revision_id)}
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        if req.outdated:
            return _redraw_outdated(pkg, takes, plans, images, req)
        ids = (
            req.shot_ids
            if req.shot_ids is not None
            else [
                s.shot_id
                for s in pkg.shot_list
                if approved_take(takes, s.shot_id, 1) is None
                and not is_busy(takes, s.shot_id)
                and not (s.shot_id in plans and plans[s.shot_id].busy)
            ]
        )
        try:
            drawn_with = runner.take_options(req.provider)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        options = {"sub_shot": 1, "draws": req.draws, "max_rounds": req.max_rounds, **drawn_with}
        started, planning, skipped = [], [], []
        scene_reading: dict[str, SceneReading | None] = {}
        for shot_id in ids:
            try:
                shot = shot_of(pkg, shot_id)
            except KeyError:
                skipped.append({"shot_id": shot_id, "reason": f"no shot {shot_id}"})
                continue
            stored = plans.get(shot_id)
            if req.plan and stored is None and not is_busy(takes, shot_id):
                # A shot that can't be drawn yet isn't planned either (a paid call).
                if missing := shot_picks(pkg, images, shot).missing_for():
                    skipped.append({"shot_id": shot_id, "reason": "first: " + "; ".join(missing)})
                    continue
                plan = ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                shot_id=shot_id, status="writing")  # fmt: skip
                repo.save_shot_plan(plan)
                runner.start_plan(plan, then=options)
                planning.append(shot_id)
                continue
            try:
                if shot.scene_id not in scene_reading:  # once per scene
                    scene_reading[shot.scene_id] = _reading(pkg, shot)
                take = plan_take(pkg, images, takes, plan_of(pkg, shot, stored), **options,
                                 reading=scene_reading[shot.scene_id])  # fmt: skip
            except (NotReady, ValueError) as exc:
                skipped.append({"shot_id": shot_id, "reason": str(exc)})
                continue
            repo.save_shot_take(take)
            takes.append(take)
            started.append(take.take_id)
        for take_id in started:
            runner.start(take_id)
        return {"started": started, "planning": planning, "skipped": skipped}

    def _drawn_from(
        pkg: ProductionPackage, shot: Shot, plan: ShotPlan, index: int, frame: str
    ) -> bool:
        """Whether sub-shot `index` has a take (for the plan as it is) drawn from `frame`,
        or one still being made."""
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if is_busy(takes, shot.shot_id, index):
            return True
        return any(
            t.shot_id == shot.shot_id and t.sub_shot == index
            and current_sub(pkg, shot, plan, t) is not None
            and t.references and t.references[0].ref_id == frame
            for t in takes
        )  # fmt: skip

    def _redraw_outdated(
        pkg: ProductionPackage,
        takes: list[ShotTake],
        plans: dict[str, ShotPlan],
        images: list[GeneratedImage],
        req: KeyframeBatchRequest,
    ) -> dict[str, Any]:
        """A new take for each shot's first out-of-date cut. A cut drawn from a design
        picture that is out of date waits until that picture is redrawn."""
        try:
            options = runner.take_options(req.provider)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        stale = outdated_keyframes(pkg, images, takes, list(plans.values()))
        by_shot: dict[str, list[ShotTake]] = {}
        for t in takes:
            if t.take_id in stale:
                by_shot.setdefault(t.shot_id, []).append(t)
        started, skipped = [], []
        scene_reading: dict[str, SceneReading | None] = {}
        for shot in pkg.shot_list:
            if shot.shot_id not in by_shot or (
                req.shot_ids is not None and shot.shot_id not in req.shot_ids
            ):
                continue
            first = min(by_shot[shot.shot_id], key=lambda t: t.sub_shot)
            if is_busy(takes, shot.shot_id):
                skipped.append({"shot_id": shot.shot_id, "reason": "a keyframe is being made"})
                continue
            if any(w.startswith(PICTURE_OUTDATED) for w in stale[first.take_id]):
                skipped.append({"shot_id": shot.shot_id, "reason": "redraw the out-of-date "
                                "design pictures first (Assets)"})  # fmt: skip
                continue
            if KF_BEFORE_OUTDATED in stale[first.take_id]:
                # Its continuity reference shows the earlier design: wait for that one.
                before = previous_shot_frame(pkg, takes, shot) if first.sub_shot == 1 else None
                which = before.role.split("(")[1].split(",")[0] if before else "the cut before"
                skipped.append({"shot_id": shot.shot_id, "reason": f"redraw and approve {which} "
                                "first: the keyframe it continues from is out of date"})  # fmt: skip
                continue
            if shot.scene_id not in scene_reading:
                scene_reading[shot.scene_id] = _reading(pkg, shot)
            try:
                take = plan_take(pkg, images, takes, plan_of(pkg, shot, plans.get(shot.shot_id)),
                                 sub_shot=first.sub_shot, draws=req.draws,
                                 max_rounds=req.max_rounds, reading=scene_reading[shot.scene_id],
                                 **options)  # fmt: skip
            except (NotReady, ValueError) as exc:
                skipped.append({"shot_id": shot.shot_id, "reason": str(exc)})
                continue
            repo.save_shot_take(take)
            takes.append(take)
            started.append(take.take_id)
        for take_id in started:
            runner.start(take_id)
        return {"started": started, "planning": [], "skipped": skipped}

    @app.post("/v1/shot-takes/{take_id}/approve", response_model=ShotTake)
    async def approve_draw(take_id: str, req: ApproveRequest) -> ShotTake:
        """Approve one draw as its sub-shot's keyframe (replacing any other approval of
        that sub-shot). A person may approve a draw the check failed; the export says so.
        With `next`, the next sub-shot's keyframe starts, drawn from this one."""
        take = _take_or_404(take_id)
        pkg = _writable(take.package_id, take.revision_id)
        d = next((d for d in take.results if d.n == req.n), None)
        if take.busy or d is None or d.status != "done":
            raise HTTPException(409, "only a finished draw of a finished take can be approved")
        shot = _shot(pkg, take.shot_id)
        stored, plan = _plan(pkg, shot)
        if stored is not None and stored.busy:
            raise HTTPException(409, f"{shot.shot_id} is being planned; approve once it is done")
        if current_sub(pkg, shot, plan, take) is None:
            raise HTTPException(409, "the sub-shot plan changed since this take was drawn")
        takes = repo.list_shot_takes(take.package_id, take.revision_id)
        if take.sub_shot > 1:
            prev = approved_take(takes, take.shot_id, take.sub_shot - 1)
            drawn_from = take.references[0].ref_id if take.references else ""
            if prev is None or drawn_from != frame_ref(prev):
                raise HTTPException(
                    409,
                    f"this keyframe was drawn from a keyframe of sub-shot {take.sub_shot - 1} "
                    "that isn't approved now",
                )
        out = repo.approve_shot_draw(take_id, req.n)
        following = plan.sub(take.sub_shot + 1)

        def takes_now() -> list[ShotTake]:
            return repo.list_shot_takes(pkg.package_id, pkg.revision_id)

        if (
            req.next
            and following is not None
            # Unless the next cut keeps an approved keyframe (one kept across an inserted cut
            # is redrawn when the user chooses: it is marked out of date).
            and approved_take(takes_now(), take.shot_id, following.index) is None
            # Unless the next cut already has a take drawn from this keyframe (takes drawn
            # from another, or for an older plan, can't be approved after it).
            and not _drawn_from(pkg, shot, plan, following.index, frame_ref(out))
        ):
            with contextlib.suppress(NotReady, ValueError):
                _start(
                    plan_take(
                        pkg,
                        repo.list_images(pkg.package_id, pkg.revision_id),
                        repo.list_shot_takes(pkg.package_id, pkg.revision_id),
                        plan,
                        sub_shot=following.index,
                        draws=take.draws,
                        max_rounds=take.max_rounds,
                        reading=_reading(pkg, shot),
                        **runner.take_options(take.provider),  # the same provider
                    )
                )
        _auto_prompt(pkg, shot)
        return out

    @app.post("/v1/experiments/{session_id}/use-keyframe", status_code=201, response_model=ShotTake)
    async def use_as_keyframe(session_id: str, req: UseKeyframeRequest) -> ShotTake:
        """A keyframe chat's image #N as a take of the sub-shot the chat is for
        (docs/library-layers.md): one draw, the chat's image, approved unless `approve` is
        false, then checked like any draw. It takes the newest take of that sub-shot drawn
        for the plan in force as its pattern (references, design, size), so it can stand
        where a drawn keyframe stands: approved, exported, and drawn from by the next cut."""
        from ..experiment import image_labels, likeness_of
        from ..shots.runner import judge_view

        session = repo.get_exp_session(session_id)
        if session is None:
            raise HTTPException(404, "chat not found")
        t = session.target
        if t is None or not t.keyframe:
            raise HTTPException(409, "this chat isn't for a keyframe")
        pkg = _writable(t.package_id, None)
        shot = _shot(pkg, t.shot_id)  # type: ignore[arg-type]
        _, plan = _plan(pkg, shot)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        if is_busy(takes, shot.shot_id, t.sub_shot):
            raise HTTPException(409, f"sub-shot {t.sub_shot} of {shot.shot_id} is still being made")
        base = max((x for x in takes if x.shot_id == shot.shot_id and x.sub_shot == t.sub_shot
                    and current_sub(pkg, shot, plan, x) is not None),
                   key=lambda x: x.created_at, default=None)  # fmt: skip
        if base is None:
            # A cut nobody has drawn yet: the take is patterned on what a draw of it would
            # be (its references, size, design), without drawing.
            images = repo.list_images(pkg.package_id, pkg.revision_id)
            reading = _reading(pkg, shot)
            try:
                base = plan_take(pkg, images, takes, plan, sub_shot=t.sub_shot or 1, draws=1,
                                 auto=False, reading=reading, **runner.take_options(None))  # fmt: skip
            except (NotReady, ValueError):
                # Not drawable now (a pick missing, the cut before out of date): the editor
                # made the picture anyway; it keeps whatever references there are.
                sub = plan.sub(t.sub_shot or 1)
                if sub is None:
                    raise HTTPException(
                        404, f"{shot.shot_id} has no sub-shot {t.sub_shot}"
                    ) from None
                refs, _ = sub_shot_references(pkg, images, takes, shot, sub, reading=reading,
                                              judge=keyframe_judge(pkg, images, takes))  # fmt: skip
                base = ShotTake(take_id="pattern", package_id=pkg.package_id,
                                revision_id=pkg.revision_id, shot_id=shot.shot_id,
                                sub_shot=sub.index, spec=sub, run_id="pattern", references=refs,
                                size=frame_size(pkg), design_print=design_print(pkg, shot),
                                likeness=any(r.likeness for r in refs))  # fmt: skip
        if req.approve and t.sub_shot and t.sub_shot > 1:
            prev = approved_take(takes, shot.shot_id, t.sub_shot - 1)
            if prev is None or (
                base.references[:1] and base.references[0].ref_id != frame_ref(prev)
            ):
                raise HTTPException(409, f"approve a keyframe of sub-shot {t.sub_shot - 1} "
                                    "and draw a take from it first")  # fmt: skip
        turns = repo.list_exp_turns(session_id)
        ref = image_labels(turns).get(req.image)
        if ref is None:
            raise HTTPException(404, f"no image #{req.image} in this chat")
        path, mime = experiments.ref_file(ref, variant="full")
        if path is None:
            raise HTTPException(409, f"image #{req.image} is not available")
        made = next((x for x in turns if ref.startswith(f"turn:{x.turn_id}:")), None)
        take_id = f"stk_{uuid.uuid4().hex[:12]}"
        ext = ".png" if mime == "image/png" else ".jpg"
        rel = f"keyframes/{pkg.package_id}/{take_id}_0{ext}"
        dest = media.path(rel)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, await asyncio.to_thread(path.read_bytes))
        thumb = await asyncio.to_thread(make_thumb, media, rel)
        view = await asyncio.to_thread(judge_view, media, rel)  # what the check sees
        recorded = likeness_of(ref, turns)
        take = base.model_copy(update={
            "take_id": take_id, "run_id": take_id, "round": 1, "status": "checking",
            "prompt": made.prompt if made else "", "prompt_source": "user", "prompt_model": None,
            "notes": "", "prompt_version": None, "warnings": [], "direction": None,
            "previous_prompt": None, "fixes": [], "auto": False, "draws": 1, "seeds": [0],
            "results": [FrameDraw(n=0, seed=0, file=rel, thumb=thumb, view=view,
                                  content_type=mime, model="chat")],
            "approved": None, "refused": False, "error": None,
            # Unknown counts as a likeness, as for a chat image used as a pick.
            "likeness": base.likeness or recorded is not False,
            "imported_from": f"experiment {session_id} #{req.image}",
            "created_at": utcnow(), "finished_at": utcnow(),
        })  # fmt: skip
        repo.save_shot_take(take)
        if req.approve:
            take = repo.approve_shot_draw(take.take_id, 0)
        runner.start_check(take.take_id, recheck=False)
        if req.approve:
            _auto_prompt(pkg, shot)
        return take

    @app.delete("/v1/shot-takes/{take_id}/approve", response_model=ShotTake)
    async def withdraw_approval(take_id: str) -> ShotTake:
        """Withdraw an approval, and the approvals of later sub-shots drawn from it."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        return repo.approve_shot_draw(take_id, None)

    @app.post("/v1/shot-takes/{take_id}/check", status_code=202, response_model=ShotTake)
    async def check_again(take_id: str) -> ShotTake:
        """Run the quality check again on the take's finished draws (e.g. after a check
        that couldn't run)."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if take.busy:
            raise HTTPException(409, "the take is still being made")
        done = [d for d in take.results if d.status == "done"]
        if not done:
            raise HTTPException(409, "the take has no finished draw to check")
        take.status = "checking"
        repo.save_shot_take(take)
        # Draws whose check couldn't run; when every draw has one, all of them again.
        runner.start_check(take_id, recheck=all(d.check is not None for d in done))
        return take

    @app.post("/v1/shot-takes/{take_id}/retry", status_code=202, response_model=ShotTake)
    async def retry_take(take_id: str) -> ShotTake:
        """Run a failed take again; finished draws are kept, failed ones are redrawn with
        their seeds."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if take.status != "failed":
            raise HTTPException(
                409, f"only a failed take can be retried (this one is {take.status})"
            )
        if take.refused:
            raise HTTPException(
                409,
                "the content rules refused this take; start a new take with another direction "
                "or prompt",
            )
        takes = repo.list_shot_takes(take.package_id, take.revision_id)
        if is_busy(takes, take.shot_id, take.sub_shot):
            raise HTTPException(
                409, f"sub-shot {take.sub_shot} of {take.shot_id} is still being made"
            )
        take.results = [d for d in take.results if d.status == "done"]  # redrawn, same seeds
        take.status, take.error, take.finished_at = "pending", None, None
        return _start(take)

    @app.post("/v1/shot-takes/{take_id}/pause", response_model=ShotTake)
    async def pause_take(take_id: str) -> ShotTake:
        """Pause a take that is being made, its automatic revision rounds included: it stops
        after the step it is on (the draw being rendered, or the prompt being written),
        keeping its finished draws. Resume goes on from there."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if not take.busy:
            raise HTTPException(409, f"only a take being made can be paused (this one is "
                                f"{take.status})")  # fmt: skip
        runner.pause(take_id)
        return take

    @app.post("/v1/shot-takes/{take_id}/resume", status_code=202, response_model=ShotTake)
    async def resume_take(take_id: str) -> ShotTake:
        """Go on with a paused take: its remaining draws, its check, then its automatic
        rounds, with the same seeds."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if take.status != "paused":
            raise HTTPException(409, f"only a paused take can be resumed (this one is "
                                f"{take.status})")  # fmt: skip
        takes = repo.list_shot_takes(take.package_id, take.revision_id)
        if is_busy(takes, take.shot_id, take.sub_shot):
            raise HTTPException(
                409, f"sub-shot {take.sub_shot} of {take.shot_id} is still being made"
            )
        take.status, take.error, take.finished_at = "pending", None, None
        return _start(take)

    @app.delete("/v1/shot-takes/{take_id}", status_code=204)
    async def delete_take(take_id: str) -> None:
        """Delete a take and its draws' files. If it held the approved keyframe, that
        approval and those of later sub-shots drawn from it are withdrawn."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if take.busy:
            raise HTTPException(409, "the take is still being made; delete it when it finishes")
        others = repo.list_shot_takes(take.package_id, take.revision_id)
        if any(
            t.busy and any(r.ref_id.startswith(f"frame:{take_id}:") for r in t.references)
            for t in others
        ):
            raise HTTPException(409, "a later sub-shot is being drawn from this take")
        if take.approved is not None:
            repo.approve_shot_draw(take_id, None)
        repo.delete_shot_take(take_id)
        for d in take.results:
            for rel in (d.file, d.thumb, d.view):
                if rel:
                    with contextlib.suppress(OSError, ValueError):
                        media.path(rel).unlink()

    @app.get("/v1/shot-takes/{take_id}/draws/{n}/{variant}", response_class=FileResponse)
    async def draw_file(take_id: str, n: int, variant: Literal["content", "thumb"]) -> FileResponse:
        take = _take_or_404(take_id)
        d = next((d for d in take.results if d.n == n), None)
        rel = d and (d.thumb if variant == "thumb" and d.thumb else d.file)
        if not rel:
            raise HTTPException(404, "no such draw")
        return file_response(rel, "image/jpeg" if rel.endswith(".jpg") else d.content_type)  # type: ignore[union-attr]
