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
from ..shot_frames import (
    DEFAULT_DRAWS,
    MAX_DRAWS,
    MAX_ROUNDS,
    NotReady,
    ShotFrameRunner,
    ShotTake,
    approved_take,
    is_busy,
    plan_take,
    shot_board,
    shot_of,
)


class KeyframeRequest(BaseModel):
    revision_id: str | None = None
    frame: Literal["start", "end"] = "start"
    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    seeds: list[int] = Field(default_factory=list, max_length=MAX_DRAWS)
    direction: str | None = Field(None, max_length=1000)
    prompt: str | None = None  # the user's own prompt (no automatic rewrite)
    auto: bool = True
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)


class KeyframeBatchRequest(BaseModel):
    revision_id: str | None = None
    shot_ids: list[str] | None = None  # None: every shot without an approved start frame
    draws: int = Field(DEFAULT_DRAWS, ge=1, le=MAX_DRAWS)
    max_rounds: int = Field(MAX_ROUNDS, ge=1, le=MAX_ROUNDS)


class ApproveRequest(BaseModel):
    n: int = Field(ge=0)


class KeyframeBoard(BaseModel):
    package_id: str
    revision_id: str
    image_model: str
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
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        return KeyframeBoard(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            image_model=image_model,
            busy=sum(1 for t in takes if t.busy),
            shots=shot_board(pkg, images, takes),
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

    @app.get("/v1/drama-packages/{package_id}/keyframes", response_model=KeyframeBoard)
    async def keyframe_board(package_id: str, revision_id: str | None = None) -> KeyframeBoard:
        """Every shot with its reference pictures, what is missing, and its keyframe takes
        (each draw with its quality check) and approved frames."""
        return _board(package_or_404(package_id, revision_id))

    @app.post(
        "/v1/drama-packages/{package_id}/keyframes/{shot_id}",
        status_code=202,
        response_model=ShotTake,
    )
    async def new_keyframe_take(package_id: str, shot_id: str, req: KeyframeRequest) -> ShotTake:
        """A new take on a shot's start or end frame: written by the model (steered by
        `direction`, revising the newest take against its check), or the user's own
        `prompt`. Drawn, checked and, with `auto`, revised until a draw passes."""
        pkg = _writable(package_id, req.revision_id)
        try:
            shot_of(pkg, shot_id)
        except KeyError:
            raise HTTPException(404, f"no shot {shot_id}") from None
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        try:
            take = plan_take(
                pkg,
                repo.list_images(pkg.package_id, pkg.revision_id),
                takes,
                shot_id,
                frame=req.frame,
                draws=req.draws,
                seeds=req.seeds,
                direction=req.direction,
                prompt=req.prompt,
                auto=req.auto,
                max_rounds=req.max_rounds,
            )
        except NotReady as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if take.prompt_source == "user":
            # A person's own prompt is screened like an edited design-board prompt (the
            # model's prompts are checked by the runner).
            try:
                await screen(
                    text_client,
                    take.prompt,
                    pictures=[
                        {"picture": f"<Picture {n}>", "kind": r.role}
                        for n, r in enumerate(take.references, 1)
                    ],
                    restrictive=True,
                )
            except GuardRefused as exc:
                raise HTTPException(422, str(exc)) from exc
        repo.save_shot_take(take)
        runner.start(take.take_id)
        return take

    @app.post("/v1/drama-packages/{package_id}/keyframes", status_code=202)
    async def keyframe_batch(package_id: str, req: KeyframeBatchRequest) -> dict[str, Any]:
        """Start frames for many shots at once (default: every shot without an approved
        start frame and with nothing running). Shots that can't be drawn yet are listed
        with the reason."""
        pkg = _writable(package_id, req.revision_id)
        takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        ids = (
            req.shot_ids
            if req.shot_ids is not None
            else [
                s.shot_id
                for s in pkg.shot_list
                if approved_take(takes, s.shot_id, "start") is None
                and not is_busy(takes, s.shot_id, "start")
            ]
        )
        started, skipped = [], []
        for shot_id in ids:
            try:
                take = plan_take(pkg, images, takes, shot_id, draws=req.draws,
                                 max_rounds=req.max_rounds)  # fmt: skip
            except (NotReady, KeyError, ValueError) as exc:
                skipped.append({"shot_id": shot_id, "reason": str(exc).strip("'")})
                continue
            repo.save_shot_take(take)
            takes.append(take)
            started.append(take.take_id)
        for take_id in started:
            runner.start(take_id)
        return {"started": started, "skipped": skipped}

    @app.post("/v1/shot-takes/{take_id}/approve", response_model=ShotTake)
    async def approve_draw(take_id: str, req: ApproveRequest) -> ShotTake:
        """Approve one draw as the shot's start or end frame (replacing any other approval
        of that frame). A person may approve a draw the check failed; the export says so."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        d = next((d for d in take.results if d.n == req.n), None)
        if take.busy or d is None or d.status != "done":
            raise HTTPException(409, "only a finished draw of a finished take can be approved")
        if take.frame == "end":
            takes = repo.list_shot_takes(take.package_id, take.revision_id)
            start = approved_take(takes, take.shot_id, "start")
            drawn_from = take.references[0].ref_id if take.references else ""
            if start is None or drawn_from != f"frame:{start.take_id}:{start.approved}":
                raise HTTPException(
                    409, "this end frame was drawn from a start frame that isn't approved now"
                )
        return repo.approve_shot_draw(take_id, req.n)

    @app.delete("/v1/shot-takes/{take_id}/approve", response_model=ShotTake)
    async def withdraw_approval(take_id: str) -> ShotTake:
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
        if is_busy(takes, take.shot_id, take.frame):
            raise HTTPException(
                409, f"the {take.frame} frame of {take.shot_id} is still being made"
            )
        take.results = [d for d in take.results if d.status == "done"]  # redrawn, same seeds
        take.status, take.error, take.finished_at = "pending", None, None
        repo.save_shot_take(take)
        runner.start(take.take_id)
        return take

    @app.delete("/v1/shot-takes/{take_id}", status_code=204)
    async def delete_take(take_id: str) -> None:
        """Delete a take and its draws' files. An end frame drawn from one of its draws
        keeps its own pictures."""
        take = _take_or_404(take_id)
        _writable(take.package_id, take.revision_id)
        if take.busy:
            raise HTTPException(409, "the take is still being made; delete it when it finishes")
        others = repo.list_shot_takes(take.package_id, take.revision_id)
        if any(t.busy and any(r.ref_id.startswith(f"frame:{take_id}:") for r in t.references)
               for t in others):  # fmt: skip
            raise HTTPException(409, "an end frame is being drawn from this take")
        if take.approved is not None:
            repo.approve_shot_draw(take_id, None)  # withdraws an end frame drawn from it too
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
