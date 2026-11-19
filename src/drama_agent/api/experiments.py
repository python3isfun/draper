"""Experiment and Face Lab routes: image chats and face studies."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..design.setup import package_has_uploads
from ..design.slots import selected_image
from ..experiment import SIZES as EXP_SIZES
from ..experiment import (
    ExpError,
    ExpSession,
    ExpTarget,
    ExpTurn,
    current_size,
    global_mentions,
    image_labels,
    import_turn,
    is_busy,
    known_refs,
    new_session,
    pick_turn,
    plan_turn,
    size_of,
)
from ..face_lab import new_study, plan_describe, plan_test, source_photo
from ..image_ids import resolve as resolve_image
from ..models import ProductionPackage
from ..models.package import utcnow
from .schemas import (
    ExpSessionCreate,
    ExpSessionItem,
    ExpSessionPatch,
    ExpSessionView,
    ExpTurnRequest,
    FaceDescribeRequest,
    FaceTestRequest,
)

log = logging.getLogger(__name__)


def register(app: FastAPI, ctx: Any) -> None:
    """Add these routes; `ctx` holds what create_app shares (repo, media, runners, helpers)."""
    _exp_session_or_404 = ctx._exp_session_or_404
    _exp_turns = ctx._exp_turns
    _file = ctx._file
    experiments = ctx.experiments
    repo = ctx.repo

    def _exp_view(session: ExpSession) -> ExpSessionView:
        turns = _exp_turns(session.session_id)
        # A size saved before chats went to 2K shows as its 2K counterpart.
        shown = session.model_copy(update={"size": current_size(session.size)})
        return ExpSessionView(session=shown, turns=turns, sizes=EXP_SIZES)

    def _messages(session_id: str) -> int:
        return sum(t.mode not in ("pick", "import") for t in repo.list_exp_turns(session_id))

    @app.get("/v1/experiments", response_model=list[ExpSessionItem])
    async def list_experiments(
        kind: Literal["chat", "face"] | None = None,
        package_id: str | None = None,
        requirement_id: str | None = None,
        deliverable_id: str | None = None,
    ) -> list[ExpSessionItem]:
        """Chats and studies, most recently active first, optionally only those for a story
        or one of its design pictures (docs/workbench.md). Each says how many messages it
        has."""

        def wanted(s: ExpSession) -> bool:
            t = s.target
            asks = (package_id, requirement_id, deliverable_id)
            if not any(asks):
                return True
            return t is not None and all(
                want in (None, have)
                for want, have in zip(asks, (t.package_id, t.requirement_id, t.deliverable_id),
                                      strict=True)
            )  # fmt: skip

        return [
            ExpSessionItem(**s.model_dump(), messages=_messages(s.session_id))
            for s in repo.list_exp_sessions()
            if kind in (None, s.kind) and wanted(s)
        ]

    def _untouched(target: ExpTarget, pick_id: str | None) -> ExpSession | None:
        """The picture's newest chat with no message yet, started from this pick (or, with
        no pick, from nothing)."""
        for s in repo.list_exp_sessions():
            if (
                s.kind != "chat"
                or s.target is None
                or (
                    (s.target.package_id, s.target.requirement_id, s.target.deliverable_id)
                    != (target.package_id, target.requirement_id, target.deliverable_id)
                )
            ):
                continue
            turns = repo.list_exp_turns(s.session_id)
            if pick_id is None and not turns:
                return s
            if pick_id is not None and len(turns) == 1 and turns[0].from_image == pick_id:
                return s
        return None

    def _target_story(target: ExpTarget, kind: str = "chat") -> ProductionPackage:
        """The story of a design picture a chat is for; 404 when it has no such picture,
        422 for a face study (Face Lab) aimed at anything but a character's face."""
        pkg = repo.get_package(target.package_id)
        if pkg is None:
            raise HTTPException(404, "story not found")
        reqmt = next((r for r in pkg.asset_requirements.items
                      if r.requirement_id == target.requirement_id), None)  # fmt: skip
        if reqmt is None or not any(
            d.deliverable_id == target.deliverable_id for d in reqmt.deliverables
        ):
            raise HTTPException(404, "no such design picture in this story")
        if kind == "face" and reqmt.asset_type != "character_identity":
            raise HTTPException(422, "a face study can only be for a character's face")
        return pkg

    @app.post("/v1/experiments", status_code=201, response_model=ExpSession)
    async def create_experiment(req: ExpSessionCreate) -> ExpSession:
        title = req.title or (req.target.label if req.target else None)
        session = new_study(title) if req.kind == "face" else new_session(title)
        pick = None
        if (target := req.target) is not None:
            pkg = _target_story(target, req.kind)
            session.target = target
            if req.kind == "chat" and req.start_from_pick:
                key = (target.requirement_id, target.deliverable_id)
                found = selected_image(repo.list_images(pkg.package_id, pkg.revision_id), key)
                pick = found if found is not None and found.file else None
            if req.kind == "chat" and req.reuse_empty:
                same = _untouched(target, pick.image_id if pick is not None else None)
                if same is not None:
                    return same
        repo.save_exp_session(session)
        if pick is not None:
            # A design picture is marked as a likeness only when drawn from one; a story
            # with uploaded photos is screened as if each picture might be one (the
            # package_has_uploads rule). Its pick starts the chat as "unknown", so the
            # chat's rules judge it like an upload rather than as fictional.
            likeness = pick.likeness or (
                None if package_has_uploads(repo, pkg.package_id) else False
            )
            try:
                await asyncio.to_thread(
                    pick_turn, repo, ctx.media, session, pick,
                    session.target.label,  # type: ignore[union-attr]
                    likeness,
                )  # fmt: skip
            except OSError:  # the pick's file is gone: the chat starts empty
                log.warning("chat %s: pick %s not copied", session.session_id, pick.image_id,
                            exc_info=True)  # fmt: skip
        return session

    def _start_face_turn(session: ExpSession, turn: ExpTurn) -> ExpTurn:
        if turn.attachments:
            # The same photo described again keeps its number.
            known = {
                ref.removeprefix("asset:"): n
                for n, ref in image_labels(repo.list_exp_turns(session.session_id)).items()
                if ref.startswith("asset:")
            }
            new = [a for a in turn.attachments if a not in known]
            fresh = iter(repo.allocate_exp_labels(session.session_id, len(new)) if new else [])
            turn.attachment_labels = [known.get(a) or next(fresh) for a in turn.attachments]
        repo.save_exp_turn(turn)
        session.updated_at = turn.created_at
        repo.save_exp_session(session)
        experiments.start(turn.turn_id)
        return turn

    @app.post("/v1/experiments/{session_id}/describe", status_code=202, response_model=ExpTurn)
    async def describe_face(session_id: str, req: FaceDescribeRequest) -> ExpTurn:
        """Face Lab: write the most descriptive text prompt for a photo (nothing drawn)."""
        session = _exp_session_or_404(session_id)
        turns = _exp_turns(session_id)
        ids = [i for i in [req.asset_id or source_photo(turns)] if i]
        assets = repo.get_source_assets(ids)
        try:
            turn = plan_describe(
                session,
                turns,
                assets,
                asset_id=req.asset_id,
                hint=req.hint,
                restrictive=req.restrictive,
            )
        except ExpError as exc:
            raise HTTPException(409 if is_busy(turns) else 422, str(exc)) from exc
        if session.title == "New face study" and req.asset_id:
            name = (assets.get(req.asset_id) or {}).get("filename") or ""
            session.title = Path(name).stem[:60] or session.title
        return _start_face_turn(session, turn)

    @app.post("/v1/experiments/{session_id}/tests", status_code=202, response_model=ExpTurn)
    async def test_face_prompt(session_id: str, req: FaceTestRequest) -> ExpTurn:
        """Face Lab: render a prompt from text alone, with fixed seeds if given."""
        session = _exp_session_or_404(session_id)
        turns = _exp_turns(session_id)
        try:
            turn = plan_test(
                session,
                turns,
                prompt=req.prompt,
                draws=req.draws,
                seeds=req.seeds,
                size=req.size,
                restrictive=req.restrictive,
            )
        except ExpError as exc:
            raise HTTPException(409 if is_busy(turns) else 422, str(exc)) from exc
        return _start_face_turn(session, turn)

    @app.get("/v1/experiments/{session_id}", response_model=ExpSessionView)
    async def get_experiment(session_id: str) -> ExpSessionView:
        return _exp_view(_exp_session_or_404(session_id))

    @app.patch("/v1/experiments/{session_id}", response_model=ExpSession)
    async def update_experiment(session_id: str, req: ExpSessionPatch) -> ExpSession:
        session = _exp_session_or_404(session_id)
        if req.title is not None:
            session.title = req.title.strip() or session.title
        if req.size is not None:
            if req.size != "auto" and req.size not in EXP_SIZES.values():
                raise HTTPException(422, f"unsupported size {req.size}")
            session.size = req.size
        if (req.clear_current or req.current is not None) and is_busy(
            repo.list_exp_turns(session_id)
        ):
            # The running message makes its result the current image when it finishes.
            raise HTTPException(409, "Wait for the current image to finish first.")
        if req.clear_current:
            session.current = None
        elif req.current is not None:
            if req.current not in known_refs(repo.list_exp_turns(session_id)):
                raise HTTPException(422, "that image is not part of this session")
            session.current = req.current
        if req.unlink:
            session.target = None
        elif req.target is not None:
            _target_story(req.target, session.kind)
            session.target = req.target
        session.updated_at = utcnow()
        repo.save_exp_session(session)
        return session

    @app.delete("/v1/experiments/{session_id}", status_code=204)
    async def delete_experiment(session_id: str) -> None:
        _exp_session_or_404(session_id)
        turns = repo.list_exp_turns(session_id)
        if is_busy(turns):
            raise HTTPException(409, "An image is still being made; delete when it's done.")
        uploads = sorted({a for t in turns for a in t.attachments})
        repo.delete_exp_session(session_id)
        await asyncio.to_thread(experiments.delete_files, session_id, uploads)

    @app.post("/v1/experiments/{session_id}/turns", status_code=202, response_model=ExpTurn)
    async def send_experiment_turn(session_id: str, req: ExpTurnRequest) -> ExpTurn:
        session = _exp_session_or_404(session_id)
        if session.kind != "chat":
            raise HTTPException(422, "This is a Face Lab study: use describe or tests.")
        turns = _exp_turns(session_id)  # numbered, so the message can mention any image
        # Uploads: this message's, plus the current image when it is an earlier upload
        # (its dimensions pick the "auto" size).
        asset_ids = list(req.attachments)
        if session.current and session.current.startswith("asset:"):
            asset_ids.append(session.current.removeprefix("asset:"))
        if is_busy(turns):
            raise HTTPException(409, "Wait for the current image to finish first.")
        _size_pick_start(session, turns)
        imports, numbers = await _bring_in(session, turns, req.text)
        try:
            turn = plan_turn(
                session,
                [*turns, *imports],
                repo.get_source_assets(asset_ids),
                text=req.text,
                attachments=req.attachments,
                mode=req.mode,
                draws=req.draws,
                size=req.size,
                restrictive=req.restrictive,
                numbers=numbers,
            )
        except ExpError as exc:
            _drop_files(imports)
            raise HTTPException(409 if is_busy(turns) else 422, str(exc)) from exc
        for t in imports:
            repo.save_exp_turn(t)
        if turn.attachments:
            turn.attachment_labels = repo.allocate_exp_labels(session_id, len(turn.attachments))
        repo.save_exp_turn(turn)
        if turn.mode == "upload":
            session.current = f"asset:{turn.attachments[0]}"
        # Titled by its first message, as chats are listed by title. A chat for a picture
        # starts with the picture's name: the panel says what it is for anyway.
        untitled = ("New chat", session.target.label if session.target else None)
        if session.title in untitled and turn.text and turn.mode != "pick":
            session.title = turn.text[:60] + ("…" if len(turn.text) > 60 else "")
        session.updated_at = turn.created_at
        repo.save_exp_session(session)
        if turn.status == "pending":
            experiments.start(turn.turn_id)
        return turn

    def _size_pick_start(session: ExpSession, turns: list[ExpTurn]) -> None:
        """Give a pick start saved without a size its picture's (any picture of the slot
        has the slot's shape), so an automatic size isn't square for a 3:4 face."""
        for t in turns:
            if t.mode != "pick" or t.size:
                continue
            img = repo.get_image(t.from_image) if t.from_image else None
            if img is None and session.target is not None:
                tg = session.target
                pkg = repo.get_package(tg.package_id)
                if pkg is not None:
                    img = selected_image(
                        repo.list_images(pkg.package_id, pkg.revision_id),
                        (tg.requirement_id, tg.deliverable_id),
                    )
            if img is not None and (size := size_of(img.size)):
                t.size = size
                repo.save_exp_turn(t)

    def _drop_files(imports: list[ExpTurn]) -> None:
        """Remove the copies unsaved import turns made."""
        for t in imports:
            for r in t.results:
                for rel in (r.file, r.thumb):
                    if rel:
                        ctx.media.path(rel).unlink(missing_ok=True)

    async def _bring_in(
        session: ExpSession, turns: list[ExpTurn], text: str
    ) -> tuple[list[ExpTurn], dict[int, int]]:
        """The images a message mentions by global number (@1234) that aren't in the chat
        yet, as unsaved "import" turns, and each mentioned number's chat number (#5). An
        image already here (made, uploaded, picked or brought in before) is reused."""
        labels = {ref: n for n, ref in image_labels(turns).items()}
        for t in turns:
            for r in t.results[:1]:
                if r.label is not None and t.mode == "import" and t.import_ref:
                    labels.setdefault(t.import_ref, r.label)
                if r.label is not None and t.mode == "pick" and t.from_image:
                    labels.setdefault(f"design:{t.from_image}", r.label)
        imports: list[ExpTurn] = []
        numbers: dict[int, int] = {}
        index = max((t.index for t in turns), default=0)
        for gid in global_mentions(text):
            ref = repo.image_ref(gid)
            if ref in labels:
                numbers[gid] = labels[ref]
                continue
            found = resolve_image(repo, ref) if ref is not None else None
            if found is None:
                _drop_files(imports)
                gone = "is no longer available" if ref else "doesn't exist"
                raise HTTPException(422, f"Image @{gid} {gone}.")
            index += 1
            t = await asyncio.to_thread(import_turn, repo, ctx.media, session, found, gid, index)
            imports.append(t)
            labels[ref] = numbers[gid] = t.results[0].label  # type: ignore[assignment]
        return imports, numbers

    def _exp_turn_or_404(turn_id: str) -> ExpTurn:
        turn = repo.get_exp_turn(turn_id)
        if turn is None:
            raise HTTPException(404, "turn not found")
        return turn

    @app.post("/v1/experiment-turns/{turn_id}/retry", status_code=202, response_model=ExpTurn)
    async def retry_experiment_turn(turn_id: str) -> ExpTurn:
        """Run a failed turn again; draws already made and a prompt already written are kept.
        A refused turn is checked again under the current rules (which may have changed)."""
        turn = _exp_turn_or_404(turn_id)
        if turn.status != "failed":
            raise HTTPException(409, "only a failed message can be retried")
        if is_busy(repo.list_exp_turns(turn.session_id)):
            raise HTTPException(409, "Wait for the current image to finish first.")
        turn.status, turn.error, turn.finished_at = "pending", None, None
        turn.refused = False
        repo.save_exp_turn(turn)
        experiments.start(turn.turn_id)
        return turn

    @app.get("/v1/experiment-turns/{turn_id}/results/{n}/{variant}", response_class=FileResponse)
    async def experiment_result_file(
        turn_id: str, n: int, variant: Literal["content", "thumb"]
    ) -> FileResponse:
        turn = _exp_turn_or_404(turn_id)
        if not 0 <= n < len(turn.results):
            raise HTTPException(404, "no such image")
        r = turn.results[n]
        if variant == "thumb" and r.thumb:
            return _file(r.thumb, "image/jpeg")
        return _file(r.file, r.content_type)
