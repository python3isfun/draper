"""HTTP endpoints of the shot keyframes (see drama_agent.shot_frames)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..content_guard import GuardRefused, screen
from ..media import MediaStore
from ..models import ProductionPackage
from ..models.package import utcnow
from ..models.storyboard import Shot
from ..shot_frames import (
    DEFAULT_DRAWS,
    MAX_CLIP_S,
    MAX_DRAWS,
    MAX_ROUNDS,
    MAX_SUB_SHOTS,
    MIN_CLIP_S,
    NotReady,
    PlanProblems,
    PromptVersion,
    ShotFrameRunner,
    ShotPlan,
    ShotTake,
    approved_take,
    current_sub,
    frame_ref,
    is_busy,
    is_hard,
    make_sub_shots,
    plan_of,
    plan_take,
    prompt_findings,
    shot_board,
    shot_of,
    shot_picks,
    stale_approvals,
    sub_shot_for,
)
from ..shot_reading import SceneReading, candidates, scene_shots


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
    # The clip's length in whole seconds (default: the storyboard's, rounded up).
    clip_s: int | None = Field(None, ge=MIN_CLIP_S, le=MAX_CLIP_S)


class ReadingRequest(BaseModel):
    revision_id: str | None = None


class ChoiceRequest(BaseModel):
    revision_id: str | None = None
    sub_shot: int = Field(1, ge=1, le=MAX_SUB_SHOTS)
    character_id: str
    image_id: str


class ApproveRequest(BaseModel):
    n: int = Field(ge=0)
    # Start the next sub-shot's keyframe (when it has none yet), with the same settings.
    next: bool = True


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
) -> None:
    def _board(pkg: ProductionPackage) -> KeyframeBoard:
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        plans = repo.list_shot_plans(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        versions = repo.list_prompt_versions(pkg.package_id, pkg.revision_id)
        readings = repo.list_scene_readings(pkg.package_id, pkg.revision_id)
        shots = shot_board(pkg, images, takes, plans, versions, readings)
        return KeyframeBoard(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            image_model=image_model,
            busy=sum(1 for t in takes if t.busy)
            + sum(1 for p in plans if p.busy)
            + sum(1 for r in readings if r.busy),
            shots=shots,
            providers={name: c.model_id for name, c in runner.providers.items()},
            default_provider=runner.default_provider,
        )

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
        plan = ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id, shot_id=shot_id)
        plan.clip_ms = req.clip_s * 1000 if req.clip_s else None
        if req.sub_shots is None:
            plan.status = "writing"
            if stored is not None:  # the board keeps showing the old one meanwhile
                plan.sub_shots, plan.source = stored.sub_shots, stored.source
            repo.save_shot_plan(plan)
            runner.start_plan(plan)
            return plan
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
        repo.save_shot_plan(plan)
        for t in stale_approvals(pkg, shot, takes, plan):
            repo.approve_shot_draw(t.take_id, None)
        return plan

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
        findings = prompt_findings(prompt, refs)
        if hard := [f for f in findings if is_hard(f)]:
            raise HTTPException(422, "The prompt " + "; ".join(hard) + ".")
        try:
            # Open-mode rules: the face and look pictures are the production's chosen
            # designs (rule 3 still applies; the runner refuses intimacy with a likeness).
            await screen(
                text_client,
                prompt,
                pictures=[
                    {"picture": f"<Picture {n}>", "kind": r.role} for n, r in enumerate(refs, 1)
                ],
                restrictive=False,
            )
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
        _, plan = _plan(pkg, shot)
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
        if (
            req.next
            and following is not None
            # Takes for an older plan's sub-shot don't count: they can't be approved.
            and not any(
                t.shot_id == take.shot_id
                and t.sub_shot == following.index
                and current_sub(pkg, shot, plan, t) is not None
                for t in takes
            )
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
        return out

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
