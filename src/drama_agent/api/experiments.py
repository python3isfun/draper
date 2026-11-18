"""Experiment and Face Lab routes: image chats and face studies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..experiment import SIZES as EXP_SIZES
from ..experiment import (
    ExpError,
    ExpSession,
    ExpTurn,
    image_labels,
    is_busy,
    known_refs,
    new_session,
    plan_turn,
)
from ..face_lab import new_study, plan_describe, plan_test, source_photo
from ..models.package import utcnow
from .schemas import (
    ExpSessionCreate,
    ExpSessionPatch,
    ExpSessionView,
    ExpTurnRequest,
    FaceDescribeRequest,
    FaceTestRequest,
)


def register(app: FastAPI, ctx: Any) -> None:
    """Add these routes; `ctx` holds what create_app shares (repo, media, runners, helpers)."""
    _exp_session_or_404 = ctx._exp_session_or_404
    _exp_turns = ctx._exp_turns
    _file = ctx._file
    experiments = ctx.experiments
    repo = ctx.repo

    def _exp_view(session: ExpSession) -> ExpSessionView:
        turns = _exp_turns(session.session_id)
        return ExpSessionView(session=session, turns=turns, sizes=EXP_SIZES)

    @app.get("/v1/experiments", response_model=list[ExpSession])
    async def list_experiments(
        kind: Literal["chat", "face"] | None = None,
    ) -> list[ExpSession]:
        return [s for s in repo.list_exp_sessions() if kind in (None, s.kind)]

    @app.post("/v1/experiments", status_code=201, response_model=ExpSession)
    async def create_experiment(req: ExpSessionCreate) -> ExpSession:
        session = new_study(req.title) if req.kind == "face" else new_session(req.title)
        repo.save_exp_session(session)
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
        try:
            turn = plan_turn(
                session,
                turns,
                repo.get_source_assets(asset_ids),
                text=req.text,
                attachments=req.attachments,
                mode=req.mode,
                draws=req.draws,
                size=req.size,
                restrictive=req.restrictive,
            )
        except ExpError as exc:
            raise HTTPException(409 if is_busy(turns) else 422, str(exc)) from exc
        if turn.attachments:
            turn.attachment_labels = repo.allocate_exp_labels(session_id, len(turn.attachments))
        repo.save_exp_turn(turn)
        if turn.mode == "upload":
            session.current = f"asset:{turn.attachments[0]}"
        if session.title == "New chat" and turn.text:
            session.title = turn.text[:60] + ("…" if len(turn.text) > 60 else "")
        session.updated_at = turn.created_at
        repo.save_exp_session(session)
        if turn.status == "pending":
            experiments.start(turn.turn_id)
        return turn

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
