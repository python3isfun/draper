"""Generating pending design pictures one at a time."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..imagegen import ImageClient, Reference
from ..media import MediaStore
from ..models import SourceAsset
from ..models.images import GeneratedImage
from ..models.package import utcnow
from .prompts import key_of
from .setup import make_thumb, package_has_uploads, story_rating
from .slots import selected_image
from .staleness import plan_refresh
from .takes import prompt_source

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Generation


class ReferenceUnavailable(RuntimeError):
    """A reference picture the prompt depends on is not available."""


# A "running" image older than this was interrupted (a crash); one in progress elsewhere is
# younger (an image request gives up after 600 s).
STALE_RUNNING_S = 1200.0


class ImageWorker:
    """Generates pending images one at a time, oldest first."""

    def __init__(
        self,
        repo: Any,
        media: MediaStore,
        client: ImageClient,
        poll_s: float = 2.0,
        writer: Any = None,
    ) -> None:
        self.repo = repo
        self.media = media
        self.client = client
        self.poll_s = poll_s
        self.writer = writer  # image_prompts.ImagePromptWriter, or None for templates

    async def run(self, stop: asyncio.Event, wakeup: asyncio.Event | None = None) -> None:
        while not stop.is_set():
            if await self.run_once():
                continue
            # Idle: pick up images a crashed process left running.
            self.repo.requeue_running_images(STALE_RUNNING_S)
            waiter = wakeup or asyncio.Event()
            try:
                await asyncio.wait_for(waiter.wait(), self.poll_s)
            except TimeoutError:
                pass
            if wakeup is not None:
                wakeup.clear()

    async def run_once(self) -> bool:
        img = self.repo.claim_next_image()
        if img is None:
            return False
        try:
            if img.option_id and not img.finalizes:  # a full-size render has its own prompt
                await self._prepare_take(img)
            elif img.prompt_source == "pending":
                await self._write_prompts(img)
            await self._generate(img)
        except asyncio.CancelledError:
            img.status, img.started_at = "pending", None  # picked up again after restart
            self.repo.save_image(img)
            raise
        except Exception as exc:  # recorded on the image; the user can regenerate
            log.warning("image %s failed", img.image_id, exc_info=True)
            img.status, img.error, img.finished_at = "failed", str(exc)[:500], utcnow()
            self.repo.save_image(img)
        return True

    async def _prepare_take(self, img: GeneratedImage) -> None:
        """An image drawn from a take: make sure the take is written (one writer call for
        every pending take of the package), then use its prompt."""
        option = self.repo.get_option(img.option_id)
        if option is None:
            raise RuntimeError(f"take {img.option_id} not found")
        if option.status == "pending":
            pkg = self.repo.get_package(img.package_id, img.revision_id)
            batch = [
                o
                for o in self.repo.list_options(img.package_id, img.revision_id)
                if o.status == "pending"
            ]
            if self.writer is not None and pkg is not None:
                await self.writer.write_takes(pkg, batch)
            else:
                for o in batch:
                    o.status = "template"
            for o in batch:
                self.repo.save_option(o)
            option = next((o for o in batch if o.option_id == option.option_id), option)
        img.prompt = option.prompt
        img.prompt_source = prompt_source(option)  # type: ignore[assignment]
        img.prompt_model = option.prompt_model
        self.repo.save_image(img)

    async def _write_prompts(self, img: GeneratedImage) -> None:
        """Have the text model write the prompts of this image and every other image of the
        same package still waiting for one: one call for the whole batch."""
        pkg = self.repo.get_package(img.package_id, img.revision_id)
        if self.writer is None or pkg is None:
            img.prompt_source = "template"
            self.repo.save_image(img)
            return
        batch = [img] + [
            i
            for i in self.repo.list_images(img.package_id, img.revision_id)
            if i.status == "pending"
            and i.prompt_source == "pending"
            and not i.option_id  # a take's draws get the take's prompt (_prepare_take)
            and i.image_id != img.image_id
        ]
        await self.writer.write(pkg, batch)
        for i in batch:
            self.repo.save_image(i)

    async def _generate(self, img: GeneratedImage) -> None:
        from ..content_guard import GuardRefused, text_guard, upload_guard

        references, missing, likeness = await asyncio.to_thread(self._references, img)
        img.likeness = img.likeness or likeness
        # Every prompt, however it was written. A job with uploaded photos may show real
        # people, and an image drawn from a picture with a real person's likeness does, so
        # those get the uploaded-photo rule.
        strict = img.likeness or package_has_uploads(self.repo, img.package_id)
        rating = story_rating(self.repo, img.package_id)
        reason = (
            upload_guard(img.prompt, rating=rating) if strict else text_guard(img.prompt, rating)
        )
        if reason:
            raise GuardRefused(f"Not drawn: {reason}")
        if missing:
            # The prompt defines each <Picture N> as a <Subject N> with its relationship;
            # without the picture it would describe something the model never receives.
            # Generate missing (or Regenerate) re-plans this image with the new reference.
            raise ReferenceUnavailable(
                f"reference image {', '.join(missing)} is not available (it failed or was "
                "never generated); generate it first, then this image"
            )
        result = await self.client.generate(img.prompt, img.size, img.seed, references)
        ext = "png" if result.content_type == "image/png" else "jpg"
        rel = f"generated/{img.package_id}/{img.image_id}.{ext}"
        await asyncio.to_thread(self._write, rel, result.data)
        img.file, img.content_type, img.model = rel, result.content_type, self.client.model_id
        img.thumb = await asyncio.to_thread(self._thumb, rel)
        img.status, img.error, img.finished_at = "done", None, utcnow()
        img.selected = False  # a pick only ever comes from set_selected / move_selection
        self.repo.save_image(img)
        if img.finalizes:
            # The full-size render of a picked draft takes over the pick, if the draft is
            # still picked (one atomic step, so it can't race with the user's next pick).
            self.repo.move_selection(img.finalizes, img.image_id)
        if img.refreshes:
            # A redraw of an out-of-date pick takes over the pick (if it is still the pick),
            # then the pictures drawn from the one it replaced are redrawn in turn.
            self.repo.move_selection(img.refreshes, img.image_id)
            pkg = self.repo.get_package(img.package_id, img.revision_id)
            if pkg is not None:
                images = self.repo.list_images(img.package_id, img.revision_id)
                for new in plan_refresh(pkg, images, drawn_from={key_of(img)}):
                    self.repo.save_image(new)

    def _references(self, img: GeneratedImage) -> tuple[list[Reference], list[str], bool]:
        """(the reference images, ids of those not available, whether any shows a real
        person's likeness)."""
        refs: list[Reference] = []
        skipped: list[str] = []
        likeness = False
        siblings: list[GeneratedImage] | None = None
        for rid in img.reference_ids:
            path, mime = None, "image/jpeg"
            if rid.startswith("sel:"):
                # Dynamic reference: the image the user selected for that deliverable.
                if siblings is None:
                    siblings = self.repo.list_images(img.package_id, img.revision_id)
                req_id, _, deliverable_id = rid[4:].partition("|")
                ref = selected_image(siblings, (req_id, deliverable_id))
                if ref and ref.file:
                    path, mime = self.media.path(ref.file), ref.content_type
                    likeness = likeness or ref.likeness
            elif rid.startswith("img_"):
                ref = self.repo.get_image(rid)
                if ref and ref.status == "done" and ref.file:
                    path, mime = self.media.path(ref.file), ref.content_type
                    likeness = likeness or ref.likeness
            else:
                raw = self.repo.get_source_asset(rid)
                if raw:
                    asset = SourceAsset.model_validate(raw)
                    if asset.kind == "image":
                        rel = asset.model_image or asset.file
                        path = self.media.path(rel)
                        mime = "image/jpeg" if asset.model_image else asset.content_type
            if path is not None and path.is_file():
                refs.append(Reference(path.name, path.read_bytes(), mime))
            else:
                skipped.append(rid)
        return refs, skipped, likeness

    def _write(self, rel: str, data: bytes) -> None:
        path = self.media.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _thumb(self, rel: str) -> str | None:
        return make_thumb(self.media, rel)
