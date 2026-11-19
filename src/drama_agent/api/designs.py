"""Design picture routes: generating, exploring, developing and picking design images, and
hand edits to the design sheets."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from ..content_guard import GuardRefused, screen, upload_guard
from ..experiment import (
    ExpSession,
    image_labels,
    likeness_of,
)
from ..image_ids import story_likeness
from ..image_prompts import problems
from ..images import (
    SIZES,
    NeedsPick,
    design_slots,
    design_text,
    draws_for,
    import_pick,
    latest_images,
    make_thumb,
    outdated_pictures,
    package_has_uploads,
    plan_images,
    plan_refresh,
    slot_title,
    story_rating,
    take_draws,
)
from ..images import develop as develop_plan
from ..images import edit_take as edit_take_plan
from ..images import explore as explore_plan
from ..images import new_take as new_take_plan
from ..images import select as select_plan
from ..models import (
    ProductionPackage,
)
from ..models.images import DesignOption, GeneratedImage
from ..sheet_edit import EditError, EntryLocked, add_variant, edit_entry
from .schemas import (
    AddVariantRequest,
    AddVariantResult,
    DesignTarget,
    DevelopRequest,
    DrawsRequest,
    EditTakeRequest,
    EntryEditRequest,
    EntryEditResult,
    ExploreRequest,
    ImagesView,
    ImportImageRequest,
    NewTakeRequest,
    ToDesignRequest,
    UsePickRequest,
)


def register(app: FastAPI, ctx: Any) -> None:
    """Add these routes; `ctx` holds what create_app shares (repo, media, runners, helpers)."""
    _exp_session_or_404 = ctx._exp_session_or_404
    _exp_turns = ctx._exp_turns
    _file = ctx._file
    _package_job = ctx._package_job
    _package_or_404 = ctx._package_or_404
    experiments = ctx.experiments
    image_wakeup = ctx.image_wakeup
    images = ctx.images
    media = ctx.media
    orchestrator = ctx.orchestrator
    repo = ctx.repo

    def _images_view(pkg: ProductionPackage, notes: list[str] | None = None) -> ImagesView:
        imgs = repo.list_images(pkg.package_id, pkg.revision_id)
        latest = latest_images(imgs)
        counts = {s: 0 for s in ("pending", "running", "done", "failed")}
        for img in latest.values():
            counts[img.status] += 1
        return ImagesView(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            image_model=images.model_id,
            images=imgs,
            options=repo.list_options(pkg.package_id, pkg.revision_id),
            slots=design_slots(pkg),
            queued=sum(1 for i in imgs if i.status in ("pending", "running")),
            notes=notes or [],
            outdated=outdated_pictures(pkg, imgs),
            **counts,
        )

    @app.post(
        "/v1/drama-packages/{package_id}/images/refresh", status_code=202, response_model=ImagesView
    )
    async def refresh_images(
        package_id: str,
        revision_id: str | None = None,
        requirement_ids: Annotated[list[str] | None, Query()] = None,
    ) -> ImagesView:
        """Redraw the out-of-date picks, in order: those drawn from up-to-date pictures now;
        each takes over its pick when done, and the pictures drawn from the one it replaced
        follow (a set's wide view, then its other states and its plan view). A hand-written
        prompt is kept; a model-written one is rewritten when the design text changed.
        `requirement_ids` limits it to those items' pictures (one character or set in the
        library); the pictures drawn from them still follow."""
        pkg = _package_or_404(package_id, revision_id)
        _package_job(pkg.package_id, None, pkg.revision_id)
        existing = repo.list_images(pkg.package_id, pkg.revision_id)
        stale = outdated_pictures(pkg, existing)
        only = set(requirement_ids) if requirement_ids else None
        new = plan_refresh(pkg, existing, only=only)
        _save_new([], new)
        later = sum(
            1 for why in stale.values() if "drawn from a picture that is out of date" in why
        )
        notes = [f"Redrawing {len(new)} out-of-date picture{'s' * (len(new) != 1)}."]
        if later:
            notes.append(f"{later} more follow once the pictures they are drawn from are redrawn.")
        return _images_view(pkg, notes)

    @app.get("/v1/drama-packages/{package_id}/images", response_model=ImagesView)
    async def list_package_images(package_id: str, revision_id: str | None = None) -> ImagesView:
        """Reference images of a package revision, every version (newest per deliverable
        is the current one), with counts over the current ones."""
        return _images_view(_package_or_404(package_id, revision_id))

    @app.post("/v1/drama-packages/{package_id}/images", status_code=202, response_model=ImagesView)
    async def generate_package_images(
        package_id: str,
        revision_id: str | None = None,
        draft: bool = False,
        job_id: str | None = None,
    ) -> ImagesView:
        """Queue images for every character, look, location and prop deliverable that has
        none yet (or whose last attempt failed). draft=true makes small, fast images.
        job_id (the job that made the package) is where the prompt writer's call is traced;
        the web UI sends it."""
        pkg = _package_or_404(package_id, revision_id)
        job = _package_job(pkg.package_id, job_id)
        existing = repo.list_images(pkg.package_id, pkg.revision_id)
        for img in plan_images(pkg, existing, draft=draft, job_id=job and job.job_id):
            repo.save_image(img)
        image_wakeup.set()
        return _images_view(pkg)

    def _save_new(opts: list[DesignOption], imgs: list[GeneratedImage]) -> None:
        for o in opts:
            repo.save_option(o)
        for i in imgs:
            repo.save_image(i)
        image_wakeup.set()

    @app.post(
        "/v1/drama-packages/{package_id}/images/explore",
        status_code=202,
        response_model=ImagesView,
    )
    async def explore_designs(package_id: str, req: ExploreRequest) -> ImagesView:
        """Phase 1: `takes` distinct designs x `draws` images for every anchor (each
        character's face, each location's and prop's first state) in scope. Pick one image
        per anchor with /select, then /develop the rest from your picks."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        opts, imgs = explore_plan(
            pkg,
            repo.list_options(pkg.package_id, pkg.revision_id),
            takes=req.takes,
            draws=req.draws,
            draft=req.draft,
            scope=list(req.scope),
            job_id=job and job.job_id,
        )
        _save_new(opts, imgs)
        return _images_view(pkg)

    @app.post(
        "/v1/drama-packages/{package_id}/images/develop",
        status_code=202,
        response_model=ImagesView,
    )
    async def develop_designs(package_id: str, req: DevelopRequest) -> ImagesView:
        """Phase 2: looks, other states and plan views, drawn from the selected anchor
        images and their takes, `draws` images each. Deliverables whose anchor has no
        selection yet are left for later (listed in `notes`)."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        opts, imgs, waiting, left_out = develop_plan(
            pkg,
            repo.list_options(pkg.package_id, pkg.revision_id),
            repo.list_images(pkg.package_id, pkg.revision_id),
            draws=req.draws,
            draft=req.draft,
            job_id=job and job.job_id,
        )
        _save_new(opts, imgs)
        notes = [f"Pick an image for {slot_title(pkg, k)} first." for k in waiting]
        return _images_view(pkg, notes + left_out)

    async def _use_chat_image(
        pkg: ProductionPackage,
        job_id: str | None,
        session: ExpSession,
        image: int,
        requirement_id: str,
        deliverable_id: str | None,
        likeness: bool | None,
    ) -> GeneratedImage:
        """Copy a chat's image #N into the story as the pick of a design picture."""
        job = _package_job(pkg.package_id, job_id, pkg.revision_id)
        turns = _exp_turns(session.session_id)
        ref = image_labels(turns).get(image)
        if ref is None:
            raise HTTPException(404, f"no image #{image} in this chat")
        reqmt = next(
            (r for r in pkg.asset_requirements.items if r.requirement_id == requirement_id),
            None,
        )
        if reqmt is None or not reqmt.deliverables:
            raise HTTPException(404, f"requirement {requirement_id} not found")
        deliverable = deliverable_id or reqmt.deliverables[0].deliverable_id
        path, mime = experiments.ref_file(ref, variant="full")
        if path is None:
            raise HTTPException(409, f"image #{image} is not available")
        recorded = likeness_of(ref, turns)
        likeness = (recorded is not False) if likeness is None else likeness
        made = next((t for t in turns if ref.startswith(f"turn:{t.turn_id}:")), None)
        ext = "png" if mime == "image/png" else "jpg"
        rel = f"generated/{pkg.package_id}/import_{session.session_id}_{image}.{ext}"
        data = await asyncio.to_thread(path.read_bytes)
        dest = media.path(rel)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, data)
        thumb = await asyncio.to_thread(make_thumb, media, rel)
        try:
            img = import_pick(
                pkg,
                (reqmt.requirement_id, deliverable),
                file=rel,
                thumb=thumb,
                content_type=mime,
                prompt=made.prompt if made else "",
                likeness=likeness,
                imported_from=f"experiment {session.session_id} #{image}",
                job_id=job and job.job_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        repo.save_image(img)
        repo.set_selected(img.image_id)
        return repo.get_image(img.image_id)

    @app.post(
        "/v1/drama-packages/{package_id}/images/import",
        status_code=201,
        response_model=GeneratedImage,
    )
    async def import_image(package_id: str, req: ImportImageRequest) -> GeneratedImage:
        """Use an Experiment image as the pick for a deliverable (e.g. a character's face).
        Develop then draws the dependent deliverables (the looks) from it. Its likeness mark
        carries over: images drawn from it get the stricter rules, and a likeness face's
        looks worn in intimate scenes are not drawn."""
        pkg = _package_or_404(package_id, req.revision_id)
        session = _exp_session_or_404(req.session_id)
        return await _use_chat_image(pkg, req.job_id, session, req.image, req.requirement_id,
                                     req.deliverable_id, req.likeness)  # fmt: skip

    @app.post("/v1/experiments/{session_id}/use", status_code=201, response_model=GeneratedImage)
    async def use_as_pick(session_id: str, req: UsePickRequest) -> GeneratedImage:
        """The workbench's Use as pick (docs/workbench.md): a chat's image #N becomes the
        pick of the design picture the chat is for, in the story's latest revision."""
        session = _exp_session_or_404(session_id)
        t = session.target
        if t is None or t.keyframe:
            raise HTTPException(409, "this chat isn't for a design picture"
                                + (" (it's a keyframe's: Use as keyframe)" if t else ""))  # fmt: skip
        pkg = _package_or_404(t.package_id, None)
        return await _use_chat_image(pkg, None, session, req.image, t.requirement_id,
                                     t.deliverable_id, req.likeness)  # fmt: skip

    @app.post(
        "/v1/drama-packages/{package_id}/images/takes",
        status_code=202,
        response_model=list[DesignOption],
    )
    async def add_take(package_id: str, req: NewTakeRequest) -> list[DesignOption]:
        """One more take on a deliverable, optionally steered (`direction`)."""
        pkg = _package_or_404(package_id, req.revision_id)
        job = _package_job(pkg.package_id, req.job_id, pkg.revision_id)
        try:
            opts, imgs = new_take_plan(
                pkg,
                repo.list_options(pkg.package_id, pkg.revision_id),
                repo.list_images(pkg.package_id, pkg.revision_id),
                (req.requirement_id, req.deliverable_id),
                direction=(req.direction or "").strip() or None,
                draws=req.draws,
                draft=req.draft,
                job_id=job and job.job_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except NeedsPick as exc:
            raise HTTPException(
                409, f"Pick an image for {slot_title(pkg, exc.key)} first."
            ) from None
        _save_new(opts, imgs)
        return opts

    @app.post(
        "/v1/image-options/{option_id}/draws",
        status_code=202,
        response_model=list[GeneratedImage],
    )
    async def more_draws(option_id: str, req: DrawsRequest) -> list[GeneratedImage]:
        """More images of an existing take (new seeds, same prompt)."""
        opt = repo.get_option(option_id)
        if opt is None:
            raise HTTPException(404, "take not found")
        pkg = _package_or_404(opt.package_id, opt.revision_id)
        _package_job(pkg.package_id, opt.job_id, pkg.revision_id)
        mine = take_draws(repo.list_images(pkg.package_id, pkg.revision_id), option_id)
        refs = mine[0].reference_ids if mine else []
        first = 1 + max((i.draw for i in mine), default=0)
        imgs = draws_for(opt, req.draws, refs, first=first)
        _save_new([], imgs)
        return imgs

    @app.post(
        "/v1/image-options/{option_id}/edit",
        status_code=202,
        response_model=DesignOption,
    )
    async def edit_take(option_id: str, req: EditTakeRequest) -> DesignOption:
        """A new take with your own wording of this take's prompt, drawn with the same seeds
        (and references) as this take's draws, so the images show what the wording changed.
        A prompt that would break the render (cites a picture the take doesn't have) is
        refused; other H3 rule misses are kept as `warnings` on the new take."""
        source = repo.get_option(option_id)
        if source is None:
            raise HTTPException(404, "take not found")
        if source.status == "pending":
            raise HTTPException(409, "this take is still being written")
        pkg = _package_or_404(source.package_id, source.revision_id)
        _package_job(pkg.package_id, source.job_id, pkg.revision_id)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        mine = take_draws(images, option_id)
        prompt = req.prompt.strip()
        n_refs = len(mine[0].reference_ids) if mine else 0
        rating = story_rating(repo, pkg.package_id)
        found = problems(prompt, n_refs, rating)
        fatal = [p for p in found if p == "empty" or p.startswith("cites a picture")]
        if fatal:
            raise HTTPException(422, f"prompt {'; '.join(fatal)}")
        # Hand-written prompts are screened. Pictures may come from the job's uploaded
        # photos (character references), so they count as possibly showing real people.
        has_uploads = package_has_uploads(repo, pkg.package_id)
        if has_uploads and (reason := upload_guard(prompt, rating=rating)):
            raise HTTPException(422, reason)
        kind = (
            "uploaded photo, or an image made from one (may show a real person)"
            if has_uploads
            else "generated image (made from text only)"
        )
        try:
            await screen(
                orchestrator.client,
                prompt,
                pictures=[
                    {"picture": f"<Picture {i}>", "kind": kind} for i in range(1, n_refs + 1)
                ],
                # The design board's images go into the hand-off package the team shoots
                # from, so it always uses the restrictive rules (the switch is Experiment's).
                restrictive=True,
                rating=rating,
            )
        except GuardRefused as exc:
            raise HTTPException(422, str(exc)) from exc
        opt, imgs = edit_take_plan(
            source,
            repo.list_options(pkg.package_id, pkg.revision_id),
            images,
            prompt,
            draws=req.draws or max(1, min(6, len(mine))),
            warnings=found,
            design=design_text(pkg, (source.requirement_id, source.deliverable_id)),
        )
        _save_new([opt], imgs)
        return opt

    @app.post(
        "/v1/generated-images/{image_id}/select",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def select_image(image_id: str) -> GeneratedImage:
        """Choose this image for its deliverable. A draft is re-rendered at full size; the
        full-size image takes over the selection when it's done (picking a draft whose
        full-size render is already done picks the render, which is returned)."""
        img = _image_or_404(image_id)
        pkg = _package_or_404(img.package_id, img.revision_id)
        _package_job(pkg.package_id, img.job_id, pkg.revision_id)
        try:
            pick, final = select_plan(repo.list_images(pkg.package_id, pkg.revision_id), image_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        repo.set_selected(pick)
        if final is not None:
            _save_new([], [final])
        return repo.get_image(pick)

    def _image_or_404(image_id: str) -> GeneratedImage:
        img = repo.get_image(image_id)
        if img is None:
            raise HTTPException(404, "image not found")
        return img

    def _delete_images(doomed: list[GeneratedImage]) -> None:
        """Remove images (and their files) with the full-size renders made from them."""
        ids = {i.image_id for i in doomed}
        if not ids:
            return
        siblings = repo.list_images(doomed[0].package_id)
        doomed = doomed + [i for i in siblings if i.finalizes in ids and i.image_id not in ids]
        if any(i.status == "running" for i in doomed):
            raise HTTPException(409, "an image is being drawn; delete it when it finishes")
        repo.delete_images([i.image_id for i in doomed])
        for i in doomed:
            for rel in (i.file, i.thumb):
                if rel:
                    with contextlib.suppress(OSError):
                        media.path(rel).unlink()

    @app.delete("/v1/generated-images/{image_id}", status_code=204)
    async def delete_image(image_id: str) -> None:
        """Delete an image (e.g. an imported pick), with its full-size render. If it was
        the pick, its item has none until you pick another."""
        _delete_images([_image_or_404(image_id)])

    @app.delete("/v1/image-options/{option_id}", status_code=204)
    async def delete_option(option_id: str) -> None:
        """Delete a take with all its draws (and their full-size renders), so it is no
        longer on the board or in the export."""
        opt = repo.get_option(option_id)
        if opt is None:
            raise HTTPException(404, "take not found")
        draws = [
            i for i in repo.list_images(opt.package_id, opt.revision_id) if i.option_id == option_id
        ]
        _delete_images(draws)
        repo.delete_option(option_id)

    def _no_running_job(package_id: str) -> None:
        if any(j.status in ("queued", "running") for j in repo.jobs_for_package(package_id)):
            raise HTTPException(409, "a job is still working on this story; edit when it's done")

    @app.patch("/v1/drama-packages/{package_id}/entries/{entry_id}", response_model=EntryEditResult)
    async def edit_package_entry(
        package_id: str, entry_id: str, req: EntryEditRequest
    ) -> EntryEditResult:
        """Edit a character, look, location, location state, prop or prop state's design
        text in place (no new revision). The asset requirements built from it are rebuilt,
        so new takes and Develop use the edit; images already drawn stay as they are."""
        pkg = _package_or_404(package_id, req.revision_id)
        _package_job(pkg.package_id, None, pkg.revision_id)
        _no_running_job(package_id)
        try:
            edit = edit_entry(pkg, entry_id, req.sheet, req.bible)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        except EntryLocked as exc:
            raise HTTPException(409, str(exc)) from None
        except EditError as exc:
            raise HTTPException(422, str(exc)) from None
        if edit.changed:
            repo.replace_package(pkg)
        affected = set(edit.requirement_ids)
        drawn = {
            (i.requirement_id, i.deliverable_id)
            for i in repo.list_images(pkg.package_id, pkg.revision_id)
            if i.requirement_id in affected and i.status == "done"
        }
        return EntryEditResult(
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            entry_id=entry_id,
            changed=edit.changed,
            requirement_ids=edit.requirement_ids,
            replaced_written=edit.replaced_written,
            drawn_before=sorted({slot_title(pkg, k) for k in drawn}),
        )

    def _lib_ids(package_id: str, kind: str, story_id: str) -> tuple[str | None, str | None]:
        """(the character or set page, the look or state on it) of a story's look or state,
        as the library has them now (a look of a linked character hangs off the shared one)."""
        from ..library import sync_library
        from ..library.models import asset_id

        sync_library(repo)
        part = repo.get_library_asset(asset_id(kind, package_id, story_id))  # type: ignore[arg-type]
        return (part.parent_id, part.asset_id) if part is not None else (None, None)

    @app.post(
        "/v1/drama-packages/{package_id}/variants",
        status_code=201,
        response_model=AddVariantResult,
    )
    async def add_package_variant(package_id: str, req: AddVariantRequest) -> AddVariantResult:
        """A new look for a character (or state for a location), added by hand in place: its
        story bible line, sheet entry and the requirement its pictures are drawn for. No
        shot wears it yet. Draw it with New take, as any other picture."""
        pkg = _package_or_404(package_id, req.revision_id)
        _package_job(pkg.package_id, None, pkg.revision_id)  # refuses archived stories
        _no_running_job(package_id)
        try:
            added = add_variant(pkg, req.parent_id, req.description, req.sheet)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from None
        except EntryLocked as exc:
            raise HTTPException(409, str(exc)) from None
        except EditError as exc:
            raise HTTPException(422, str(exc)) from None
        repo.replace_package(pkg)
        is_look = added.entry_id in {lk.look_id for lk in pkg.story_bible.looks}
        lib, part = _lib_ids(pkg.package_id, "look" if is_look else "set_state", added.entry_id)
        return AddVariantResult(
            package_id=pkg.package_id, revision_id=pkg.revision_id, entry_id=added.entry_id,
            requirement_id=added.requirement_id, deliverable_ids=added.deliverable_ids,
            lib_asset_id=lib, part_asset_id=part,
        )  # fmt: skip

    # -- Keyframe draws as takes of a look or a set state ---------------------------------

    def _keyframe(take_id: str, n: int) -> tuple[Any, Any, ProductionPackage]:
        """(the take, its draw, the story's latest revision), or 404."""
        take = repo.get_shot_take(take_id)
        d = next((d for d in take.results if d.n == n), None) if take else None
        if take is None or d is None or d.status != "done" or not d.file:
            raise HTTPException(404, "no such keyframe draw")
        return take, d, _package_or_404(take.package_id, None)

    def _keyframe_tag(take: Any, n: int) -> str:
        return f"keyframe {take.take_id}:{n} (shot {take.shot_id})"

    def _targets(take: Any, pkg: ProductionPackage) -> list[tuple[str, tuple[str, str], str]]:
        """(kind, slot, story id) of each look the shot's characters wear and its set
        state's wide view, among the story's design pictures."""
        from ..pipeline.asset_aggregation import ReuseUnit

        shot = next((s for s in pkg.shot_list if s.shot_id == take.shot_id), None)
        if shot is None:
            return []
        reqs = {r.requirement_id: r for r in pkg.asset_requirements.items}
        out = []
        units = [("look", ReuseUnit("character_look", "character", r.character_id, r.look_id), r.look_id)
                 for r in shot.character_look_refs]  # fmt: skip
        if shot.location_state_id:
            units.append(("set", ReuseUnit("location", "location", shot.location_id,
                                           shot.location_state_id), shot.location_state_id))  # fmt: skip
        for kind, unit, story_id in units:
            req = reqs.get(unit.requirement_id)
            if req is None or not req.deliverables:
                continue
            d = next((d for d in req.deliverables if d.deliverable_id.endswith("_wide")),
                     req.deliverables[0])  # fmt: skip
            out.append((kind, (req.requirement_id, d.deliverable_id), story_id))
        return out

    @app.get("/v1/shot-takes/{take_id}/draws/{n}/design-targets", response_model=list[DesignTarget])
    async def keyframe_targets(take_id: str, n: int) -> list[DesignTarget]:
        """Where this keyframe draw can go as a take: the looks its shot's characters wear
        and its set state, each saying whether it was already sent there."""
        take, _, pkg = _keyframe(take_id, n)
        tag = _keyframe_tag(take, n)
        imgs = repo.list_images(pkg.package_id, pkg.revision_id)
        out = []
        for kind, key, story_id in _targets(take, pkg):
            sent = next((i for i in imgs if (i.requirement_id, i.deliverable_id) == key
                         and i.imported_from == tag), None)  # fmt: skip
            lib, part = _lib_ids(
                pkg.package_id, "look" if kind == "look" else "set_state", story_id
            )
            out.append(DesignTarget(
                requirement_id=key[0], deliverable_id=key[1], kind=kind,  # type: ignore[arg-type]
                label=slot_title(pkg, key), sent_image_id=sent and sent.image_id,
                picked=bool(sent and sent.selected), lib_asset_id=lib, part_asset_id=part,
            ))  # fmt: skip
        return out

    @app.post(
        "/v1/shot-takes/{take_id}/draws/{n}/to-design",
        status_code=201,
        response_model=GeneratedImage,
    )
    async def keyframe_to_design(take_id: str, n: int, req: ToDesignRequest) -> GeneratedImage:
        """A keyframe draw as a take of a look its shot wears (or of its set state): copied
        in beside the drawn takes, marked where it came from. It becomes the pick only with
        `pick`. Sent again, the copy already there is used."""
        take, d, pkg = _keyframe(take_id, n)
        job = _package_job(pkg.package_id, None, pkg.revision_id)  # refuses archived stories
        key = (req.requirement_id, req.deliverable_id)
        if key not in {k for _, k, _ in _targets(take, pkg)}:
            raise HTTPException(422, "send it to a look its shot wears or to its set state")
        tag = _keyframe_tag(take, n)
        img = next((i for i in repo.list_images(pkg.package_id, pkg.revision_id)
                    if (i.requirement_id, i.deliverable_id) == key and i.imported_from == tag),
                   None)  # fmt: skip
        if img is None:
            path = media.path(d.file)
            ext = "png" if d.content_type == "image/png" else "jpg"
            rel = f"generated/{pkg.package_id}/keyframe_{take.take_id}_{n}.{ext}"
            data = await asyncio.to_thread(path.read_bytes)
            dest = media.path(rel)
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(dest.write_bytes, data)
            thumb = await asyncio.to_thread(make_thumb, media, rel)
            # Unknown (a story with uploaded photos) counts as a likeness, as for a chat's.
            likeness = story_likeness(repo, pkg.package_id, take.likeness) is not False
            try:
                img = import_pick(pkg, key, file=rel, thumb=thumb, content_type=d.content_type,
                                  prompt=take.prompt, likeness=likeness, imported_from=tag,
                                  job_id=job and job.job_id)  # fmt: skip
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from None
            repo.save_image(img)
        if req.pick:
            repo.set_selected(img.image_id)
        return repo.get_image(img.image_id)

    @app.post(
        "/v1/generated-images/{image_id}/regenerate",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def regenerate_image(image_id: str, draft: bool | None = None) -> GeneratedImage:
        """A new version of this image's deliverable, with a fresh seed. References use the
        current versions of the images it depends on."""
        old = _image_or_404(image_id)
        pkg = _package_or_404(old.package_id, old.revision_id)
        _package_job(pkg.package_id, old.job_id)
        small = draft if draft is not None else old.size in {s[1] for s in SIZES.values()}
        [new] = plan_images(
            pkg,
            repo.list_images(pkg.package_id, pkg.revision_id),
            draft=small,
            only=(old.requirement_id, old.deliverable_id),
            job_id=old.job_id,
        )
        repo.save_image(new)
        image_wakeup.set()
        return new

    @app.post(
        "/v1/generated-images/{image_id}/retry",
        status_code=202,
        response_model=GeneratedImage,
    )
    async def retry_image(image_id: str) -> GeneratedImage:
        """Queue a failed image again as it was: same prompt, seed, size and references (a
        failed draw of a take stays that draw)."""
        img = _image_or_404(image_id)
        pkg = _package_or_404(img.package_id, img.revision_id)
        _package_job(pkg.package_id, img.job_id, pkg.revision_id)
        if img.status != "failed":
            raise HTTPException(
                409, f"only a failed image can be retried (this one is {img.status})"
            )
        img.status, img.error, img.started_at, img.finished_at = "pending", None, None, None
        repo.save_image(img)
        image_wakeup.set()
        return img

    @app.get("/v1/generated-images/{image_id}", response_model=GeneratedImage)
    async def get_image(image_id: str) -> GeneratedImage:
        return _image_or_404(image_id)

    @app.get("/v1/generated-images/{image_id}/{variant}", response_class=FileResponse)
    async def image_file(image_id: str, variant: Literal["content", "thumb"]) -> FileResponse:
        img = _image_or_404(image_id)
        rel = img.thumb if variant == "thumb" and img.thumb else img.file
        if rel is None:
            raise HTTPException(404, f"image is {img.status}")
        return _file(rel, "image/jpeg" if rel.endswith(".jpg") else img.content_type)
