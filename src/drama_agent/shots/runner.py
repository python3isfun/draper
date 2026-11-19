"""Running takes and plans in the API process: reading the scene, writing, drawing,
checking and revising, one render at a time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any

from .. import shot_reading
from ..content_guard import GuardRefused, text_guard, upload_guard
from ..experiment import SIZES
from ..imagegen import ImageClient, Reference
from ..images import make_thumb, package_has_uploads, story_rating
from ..llm import GenerationRequest
from ..media import MediaPart, MediaStore
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.package import utcnow
from ..models.storyboard import Shot
from ..shot_reading import SceneReading
from .approvals import stale_approvals
from .common import KEYFRAME_SIZES, NotReady, intimate_scene, shot_of
from .judge import JudgeOutput, judge_instructions
from .models import (
    CHECK_STAGE,
    DEFAULT_PROVIDER,
    JUDGE_PX,
    PLAN_STAGE,
    WRITE_STAGE,
    FrameCheck,
    FrameDraw,
    PromptVersion,
    ShotPlan,
    ShotTake,
    TakeStatus,
    score,
)
from .planner import PlanOutput, plan_context, plan_instructions, planned_sub_shots
from .plans import TOO_LONG, check_plan
from .references import sub_shot_references
from .staleness import design_print, keyframe_judge
from .takes import next_round, plan_take
from .writer import (
    FramePromptOutput,
    is_hard,
    picture_sentences,
    prompt_findings,
    shot_context,
    sub_shot_context,
    take_states,
    take_sub,
    template_prompt,
    worth_repair,
    writer_instructions,
)

# ---------------------------------------------------------------------------
# Runner


log = logging.getLogger(__name__)


class ShotFrameRunner:
    """Runs shot takes in the API process, round after round, one render at a time (it
    shares the GPU lock with the Experiment tab). Unfinished takes don't survive a restart:
    `recover` marks them failed."""

    def __init__(
        self,
        repo: Any,
        media: MediaStore,
        images: ImageClient,
        text_client: Any,
        gpu: asyncio.Lock | None = None,
        providers: dict[str, ImageClient] | None = None,
        default_provider: str = DEFAULT_PROVIDER,
    ) -> None:
        self.repo = repo
        self.media = media
        self.images = images
        # Every provider a keyframe can be drawn with: H3 always, others when configured.
        self.providers: dict[str, ImageClient] = {DEFAULT_PROVIDER: images, **(providers or {})}
        self.default_provider = (
            default_provider if default_provider in self.providers else DEFAULT_PROVIDER
        )
        self.text = text_client
        self._gpu = gpu or asyncio.Lock()
        self._planning = asyncio.Semaphore(4)  # sub-shot plans written at once
        self._reading_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.pausing: set[str] = set()  # takes asked to pause, by id

    def provider(self, name: str | None) -> tuple[str, ImageClient]:
        """A provider by name (None: the default), or ValueError naming the ones there are."""
        name = name or self.default_provider
        if name not in self.providers:
            raise ValueError(f"no image provider {name!r}; there are: {', '.join(self.providers)}")
        return name, self.providers[name]

    def take_options(self, name: str | None) -> dict[str, Any]:
        """plan_take's provider arguments for a provider by name."""
        name, client = self.provider(name)
        return {"provider": name, "max_references": client.max_references}

    def recover(self) -> int:
        error = "Interrupted by a server restart. Retry to run it again."
        return (
            self.repo.fail_unfinished_shot_takes(error)
            + self.repo.fail_unfinished_shot_plans(error)
            + self.repo.fail_unfinished_scene_readings(error)
        )

    # -- reading the scene (shot_reading.py) ----------------------------------

    def scene_inputs(
        self, pkg: ProductionPackage
    ) -> tuple[list[GeneratedImage], dict[str, ShotPlan]]:
        images = self.repo.list_images(pkg.package_id, pkg.revision_id)
        plans = {p.shot_id: p for p in self.repo.list_shot_plans(pkg.package_id, pkg.revision_id)}
        return images, plans

    def fresh_reading(self, pkg: ProductionPackage, scene_id: str) -> SceneReading | None:
        images, plans = self.scene_inputs(pkg)
        return shot_reading.fresh(
            self.repo.get_scene_reading(pkg.package_id, pkg.revision_id, scene_id),
            shot_reading.fingerprint(pkg, scene_id, plans, images),
        )

    def start_reading(self, pkg: ProductionPackage, scene_id: str) -> None:
        """Read the scene again in the background (the board's "Read the scene again")."""
        task = asyncio.create_task(self.ensure_reading(pkg, scene_id, force=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def ensure_reading(
        self, pkg: ProductionPackage, scene_id: str, *, force: bool = False
    ) -> SceneReading | None:
        """The scene's fresh reading, read now if it is missing or stale (once per scene at
        a time). None when the scene has no pictures to choose from or the reading failed:
        the storyboard's looks are used then."""
        key = (pkg.package_id, pkg.revision_id, scene_id)
        lock = self._reading_locks.setdefault(key, asyncio.Lock())
        async with lock:
            images, plans = self.scene_inputs(pkg)
            print_ = shot_reading.fingerprint(pkg, scene_id, plans, images)
            stored = self.repo.get_scene_reading(*key)
            if not force and (ready := shot_reading.fresh(stored, print_)) is not None:
                return ready
            return await self._read_scene(pkg, scene_id, plans, images, print_, stored)

    async def _read_scene(
        self,
        pkg: ProductionPackage,
        scene_id: str,
        plans: dict[str, ShotPlan],
        images: list[GeneratedImage],
        print_: str,
        stored: SceneReading | None,
    ) -> SceneReading | None:
        people = sorted(
            {c for s in shot_reading.scene_shots(pkg, scene_id) for c in s.character_ids}
        )
        pool = [c for who in people for c in shot_reading.candidates(pkg, images, who)]
        if not pool:
            return None
        reading = SceneReading(package_id=pkg.package_id, revision_id=pkg.revision_id,
                               scene_id=scene_id, status="writing", fingerprint=print_,
                               candidates=pool)  # fmt: skip
        if stored is not None:  # the board keeps showing the old choices meanwhile
            reading.choices = stored.choices
        self.repo.save_scene_reading(reading)
        media = []
        for i, c in enumerate(pool, 1):
            found = self._ref_file(c.image_id, "thumb")
            if found is not None and found[0].is_file():
                media.append(MediaPart(label=f"<Image {i}>", path=found[0], mime=found[1]))
        context = shot_reading.reading_context(pkg, scene_id, plans, pool)
        try:
            for attempt in range(2):  # once more with what was wrong
                request = GenerationRequest(
                    stage=shot_reading.STAGE,  # type: ignore[arg-type]
                    instructions=shot_reading.instructions(),
                    context=context,
                    media=media,
                    attempt=attempt,
                )
                out = await self.text.generate(request, shot_reading.ReadingOutput)
                try:
                    pool, choices = shot_reading.read_output(pkg, scene_id, plans, pool, out)
                    break
                except ValueError as exc:
                    if attempt:
                        raise
                    context = context | {"problems": str(exc).split("; ")}
            reading.candidates = pool
            # The person's choices as they are now (choices can't change while a scene is read).
            latest = self.repo.get_scene_reading(pkg.package_id, pkg.revision_id, scene_id)
            reading.choices = shot_reading.merge_overrides(choices, latest or stored, pool)
            reading.model = request.model or getattr(self.text, "model_id", None)
            reading.status, reading.error = "done", None
        except Exception as exc:
            log.warning("scene reading failed for %s", scene_id, exc_info=True)
            error = f"The scene couldn't be read: {str(exc)[:300]}"
            if stored is not None and stored.choices and stored.status != "failed":
                # The last good reading stays in use (with the person's choices), the failure
                # shown beside it.
                reading = stored.model_copy(update={"status": "done", "error": error})
            else:
                reading.status, reading.error = "failed", error
        reading.updated_at = utcnow()
        self.repo.save_scene_reading(reading)
        return reading if reading.status == "done" else None

    async def _choose_pictures(self, pkg: ProductionPackage, shot: Shot, take: ShotTake) -> None:
        """A written take's pictures, chosen by reading its scene (read now if needed)."""
        reading = await self.ensure_reading(pkg, shot.scene_id)
        if reading is None:
            return
        images = self.repo.list_images(pkg.package_id, pkg.revision_id)
        takes = self.repo.list_shot_takes(pkg.package_id, pkg.revision_id)
        refs, missing = sub_shot_references(
            pkg,
            images,
            takes,
            shot,
            take_sub(pkg, shot, take),
            reading=reading,
            judge=keyframe_judge(pkg, images, takes),
        )
        if not missing and refs:
            take.references, take.likeness = refs, any(r.likeness for r in refs)
            self.repo.save_shot_take(take)

    def start(self, take_id: str) -> None:
        task = asyncio.create_task(self.run(take_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start_plan(self, plan: ShotPlan, then: dict[str, Any] | None = None) -> None:
        """Write a shot's sub-shot plan in the background (the API has saved it as
        "writing"); with `then` (plan_take's options), start sub-shot 1 afterwards."""
        task = asyncio.create_task(self._plan(plan, then))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _plan(self, plan: ShotPlan, then: dict[str, Any] | None) -> None:
        async with self._planning:
            try:
                await self._write_plan(plan)
            except asyncio.CancelledError:
                plan.status, plan.error = "failed", "Interrupted. Plan it again."
                self.repo.save_shot_plan(plan)
                raise
        if then is None or plan.status != "done":
            return
        pkg = self.repo.get_package(plan.package_id, plan.revision_id)
        if pkg is None:
            return
        try:
            take = plan_take(
                pkg,
                self.repo.list_images(plan.package_id, plan.revision_id),
                self.repo.list_shot_takes(plan.package_id, plan.revision_id),
                plan,
                **then,
            )
        except (NotReady, ValueError) as exc:
            log.info("no keyframe started for %s after planning: %s", plan.shot_id, exc)
            return
        self.repo.save_shot_take(take)
        await self.run(take.take_id)

    async def _write_plan(self, plan: ShotPlan) -> None:
        pkg = self.repo.get_package(plan.package_id, plan.revision_id)
        try:
            if pkg is None:
                raise RuntimeError("the package revision is gone")
            shot = shot_of(pkg, plan.shot_id)
            context = plan_context(pkg, shot, plan.clip_ms)
            for attempt in range(2):  # once more with the problems of a plan that isn't clean
                request = GenerationRequest(
                    stage=PLAN_STAGE,  # type: ignore[arg-type]
                    instructions=plan_instructions(),
                    context=context,
                    attempt=attempt,
                )
                out = await self.text.generate(request, PlanOutput)
                try:
                    subs = planned_sub_shots(pkg, shot, out, plan.clip_ms, strict=False)
                    problems = check_plan(pkg, shot, subs)
                except ValueError as exc:  # not a plan at all: retry, then give up
                    if attempt:
                        raise
                    subs, problems = None, [str(exc)]
                # Dialogue held in one cut is the planner's to fix (the check already spares a
                # storyboard that holds the two-shot); a shot too long for H3 isn't.
                fixable = [p for p in problems if not p.startswith(TOO_LONG)]
                if subs is not None and (not fixable or attempt):
                    # Clean, or still not after the retry: kept, and flagged until fixed.
                    plan.sub_shots = subs
                    break
                context = context | {"previous_plan": out.model_dump(), "problems": fixable}
            plan.notes, plan.source = out.notes.strip(), "model"
            plan.model = request.model or getattr(self.text, "model_id", None)
            if pkg is not None:
                plan.design_print = design_print(pkg, shot_of(pkg, plan.shot_id))
            plan.status, plan.error = "done", None
        except Exception as exc:  # the previous plan (if any) stays; the user can plan again
            log.warning("sub-shot plan failed for %s", plan.shot_id, exc_info=True)
            plan.status, plan.error = "failed", f"The plan couldn't be written: {str(exc)[:300]}"
        plan.updated_at = utcnow()
        self.repo.save_shot_plan(plan)
        if plan.status == "done" and pkg is not None:
            shot = shot_of(pkg, plan.shot_id)
            takes = self.repo.list_shot_takes(plan.package_id, plan.revision_id)
            for t in stale_approvals(pkg, shot, takes, plan):
                self.repo.approve_shot_draw(t.take_id, None)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def run(self, take_id: str) -> None:
        """A take, then its automatic next rounds until a draw passes or none are left, or
        the user pauses it (between steps: a round paused before its next round starts
        leaves that round paused, so resuming goes on with the revision)."""
        take = self.repo.get_shot_take(take_id)
        try:
            await self._rounds(take)
        finally:
            # A pause asked too late to apply (the take had finished) is dropped.
            if take is not None:
                stopped = self.repo.list_shot_takes(take.package_id, take.revision_id)
                self.pausing -= {t.take_id for t in stopped if not t.busy}

    async def _rounds(self, take: ShotTake | None) -> None:
        while take is not None and take.status == "pending":
            await self._run_take(take)
            nxt = next_round(take) if take.status == "done" else None
            if nxt is None:
                break
            if take.take_id in self.pausing:
                # Paused as this round finished: the next round is kept, paused.
                self.pausing.discard(take.take_id)
                nxt.status, nxt.finished_at = "paused", utcnow()
            self.repo.save_shot_take(nxt)
            take = nxt

    def pause(self, take_id: str) -> None:
        """Ask a running take to pause after the step it is on (kept in memory: the take's
        own saves would overwrite a stored flag, and a restart fails running takes anyway)."""
        self.pausing.add(take_id)

    def _pause_point(self, take: ShotTake) -> bool:
        """Pause here if the user asked: the take is saved as paused, keeping its finished
        draws and prompt. Returns whether it paused."""
        if take.take_id not in self.pausing:
            return False
        self.pausing.discard(take.take_id)
        take.status, take.finished_at = "paused", utcnow()
        self.repo.save_shot_take(take)
        return True

    async def _run_take(self, take: ShotTake) -> None:
        pkg = self.repo.get_package(take.package_id, take.revision_id)
        try:
            if pkg is None:
                raise RuntimeError("the package revision is gone")
            shot = shot_of(pkg, take.shot_id)
            if self._pause_point(take):
                return
            if not take.prompt:
                self._set(take, "writing")
                await self._choose_pictures(pkg, shot, take)
                await self._write(pkg, shot, take)
                if self._pause_point(take):
                    return
            self._guard(pkg, shot, take)
            refs = await asyncio.to_thread(self._reference_bytes, take)
            self._set(take, "drawing")
            take.results = [d for d in take.results if d.status == "done"]
            for n in range(take.draws):
                if any(d.n == n for d in take.results):
                    continue  # kept from before a retry or a pause
                if self._pause_point(take):
                    return
                take.results.append(await self._draw(take, n, refs))
                take.results.sort(key=lambda d: d.n)
                self.repo.save_shot_take(take)
            if self._pause_point(take):
                return
            self._set(take, "checking")
            await self.check(pkg, shot, take)
            take.status, take.error = "done", None
        except asyncio.CancelledError:
            take.status, take.error = "failed", "Interrupted. Retry to run it again."
            take.finished_at = utcnow()
            self.repo.save_shot_take(take)
            raise
        except GuardRefused as exc:
            take.status, take.refused, take.error = "failed", True, str(exc)
        except Exception as exc:  # recorded on the take; the user can retry
            log.warning("shot take %s failed", take.take_id, exc_info=True)
            take.status, take.error = "failed", str(exc)[:500]
        take.finished_at = utcnow()
        self.repo.save_shot_take(take)

    def _set(self, take: ShotTake, status: TakeStatus) -> None:
        take.status = status
        self.repo.save_shot_take(take)

    # -- writing ------------------------------------------------------------

    async def _write(self, pkg: ProductionPackage, shot: Shot, take: ShotTake) -> None:
        """Have the writer write the prompt; if it breaks the H3 image rules, once more
        with the findings. Hard problems (a picture it can't cite, empty, too long, not
        allowed) fall back to the template; other findings stay on the take as warnings.
        The prompt becomes the sub-shot's next version."""
        template = template_prompt(pkg, take)
        pictures = picture_sentences(pkg, take)
        rating = story_rating(self.repo, pkg.package_id)
        # The program's own sentences: the wording checks leave them to their authors.
        fixed = tuple(x[k] for x in pictures for k in ("define", "relationship"))
        context = {
            "sub_shot": sub_shot_context(
                pkg, shot, take_sub(pkg, shot, take), self._of(take), take_states(take)
            ),
            "pictures": pictures,
            **shot_context(pkg, shot),
            "aspect": next(
                (k for k, v in {**SIZES, **KEYFRAME_SIZES}.items() if v == take.size), take.size
            ),
            "previous_prompt": take.previous_prompt,
            "fixes": take.fixes,
            "direction": take.direction,
            "template_prompt": template,
        }
        media = self._media(take)
        first = context
        tries: list[tuple[str, FramePromptOutput, list[str], GenerationRequest]] = []
        error: Exception | None = None
        for attempt in range(2):
            request = GenerationRequest(
                stage=WRITE_STAGE,  # type: ignore[arg-type]
                instructions=writer_instructions(rating),
                context=context,
                media=media,
                attempt=attempt,
            )
            try:
                out = await self.text.generate(request, FramePromptOutput)
            except Exception as exc:
                log.warning("shot frame writer failed for %s", take.take_id, exc_info=True)
                error = exc
                break
            prompt = out.prompt.strip()
            findings = prompt_findings(prompt, take.references, fixed, rating)
            tries.append((prompt, out, findings, request))
            if not worth_repair(findings):
                break
            # The repair keeps what this round is for (the check's findings) and adds the
            # rule check's.
            context = first | {
                "previous_prompt": prompt,
                "fixes": [*take.fixes, *(f"The rule check: {f}" for f in findings)],
            }
        usable = [t for t in tries if not any(is_hard(f) for f in t[2])]
        if not usable:
            why = str(error) if error else "; ".join(f for f in tries[-1][2] if is_hard(f))
            take.prompt, take.prompt_source = template, "template"
            take.warnings = prompt_findings(template, take.references, fixed, rating)
            take.notes = f"The prompt writer failed ({why[:120]}); using the built-in template."
            self._version(take, "template", None)
            return
        # The attempt with the fewest findings (the later one on a tie).
        prompt, out, findings, request = min(reversed(usable), key=lambda t: len(t[2]))
        take.prompt, take.prompt_source = prompt, "model"
        take.prompt_model = request.model or getattr(self.text, "model_id", None)
        take.notes, take.motion = out.notes.strip(), out.motion.strip()
        take.warnings = findings
        self._version(take, "model", take.prompt_model)
        self.repo.save_shot_take(take)

    def _version(self, take: ShotTake, source: str, model: str | None) -> None:
        """Record the take's new prompt as the sub-shot's next version."""
        if take.spec is None:
            return
        ver = self.repo.add_prompt_version(
            PromptVersion(
                package_id=take.package_id, revision_id=take.revision_id,
                shot_id=take.shot_id, sub_shot=take.sub_shot, spec=take.spec,
                prompt=take.prompt, source=source, model=model, note=take.notes,  # type: ignore[arg-type]
                take_id=take.take_id, findings=take.warnings, references=take.references,
            )
        )  # fmt: skip
        take.prompt_version = ver.v

    def _of(self, take: ShotTake) -> int:
        """How many sub-shots the take's shot has now."""
        plan = self.repo.get_shot_plan(take.package_id, take.revision_id, take.shot_id)
        return len(plan.sub_shots) if plan is not None and plan.sub_shots else 1

    def _guard(self, pkg: ProductionPackage, shot: Shot, take: ShotTake) -> None:
        level = intimate_scene(pkg, shot)
        if level and take.likeness:
            raise GuardRefused(
                f"Not drawn: this shot is in a scene staged as {level} intimacy, and a picked "
                "design image shows a real person's likeness (content rule 3). Pick a design "
                "image without a likeness for the people in it."
            )
        strict = take.likeness or package_has_uploads(self.repo, take.package_id)
        rating = story_rating(self.repo, take.package_id)
        reason = (
            upload_guard(take.prompt, rating=rating) if strict else text_guard(take.prompt, rating)
        )
        if reason:
            raise GuardRefused(f"Not drawn: {reason}")

    # -- drawing ------------------------------------------------------------

    def _ref_file(self, ref_id: str, variant: str = "file") -> tuple[Any, str] | None:
        """(path, mime) of a reference picture, or None."""
        if ref_id.startswith("frame:"):
            _, take_id, n = ref_id.split(":")
            src = self.repo.get_shot_take(take_id)
            d = next((d for d in src.results if d.n == int(n)), None) if src else None
            rel = d and (d.thumb if variant == "thumb" and d.thumb else d.file)
            if not rel or d is None:
                return None
            # A thumbnail is a JPEG whatever the draw's own type.
            return self.media.path(rel), "image/jpeg" if rel.endswith(".jpg") else d.content_type
        img = self.repo.get_image(ref_id)
        if img is None or img.status != "done" or not img.file:
            return None
        rel = img.thumb if variant == "thumb" and img.thumb else img.file
        mime = "image/jpeg" if rel.endswith(".jpg") else img.content_type
        return self.media.path(rel), mime

    def _reference_bytes(self, take: ShotTake) -> list[Reference]:
        out = []
        for r in take.references:
            found = self._ref_file(r.ref_id)
            if found is None or not found[0].is_file():
                raise RuntimeError(f"reference picture {r.role} is not available any more")
            path, mime = found
            out.append(Reference(path.name, path.read_bytes(), mime))
        return out

    def _media(self, take: ShotTake) -> list[MediaPart]:
        parts = []
        for n, r in enumerate(take.references, 1):
            found = self._ref_file(r.ref_id, "thumb")
            if found is not None and found[0].is_file():
                parts.append(MediaPart(label=f"<Picture {n}>", path=found[0], mime=found[1]))
        return parts

    async def _draw(self, take: ShotTake, n: int, refs: list[Reference]) -> FrameDraw:
        seed = take.seeds[n] if n < len(take.seeds) else random.randint(1, 2**31 - 1)
        client = self.providers.get(take.provider)
        if client is None:
            return FrameDraw(n=n, seed=seed, status="failed",
                             error=f"the image provider {take.provider!r} isn't configured")  # fmt: skip
        try:
            # The GPU lock is H3's (shared with the Experiment tab); a remote API needs none.
            lock = self._gpu if take.provider == DEFAULT_PROVIDER else contextlib.nullcontext()
            async with lock:
                result = await client.generate(take.prompt, take.size, seed, refs)
        except Exception as exc:
            log.warning("shot draw %s/%d failed", take.take_id, n, exc_info=True)
            return FrameDraw(n=n, seed=seed, status="failed", error=str(exc)[:300])
        ext = "png" if result.content_type == "image/png" else "jpg"
        rel = f"keyframes/{take.package_id}/{take.take_id}_{n}.{ext}"
        await asyncio.to_thread(self._write_file, rel, result.data)
        thumb = await asyncio.to_thread(make_thumb, self.media, rel)
        view = await asyncio.to_thread(judge_view, self.media, rel)
        return FrameDraw(n=n, seed=seed, file=rel, thumb=thumb, view=view,
                         content_type=result.content_type, model=client.model_id)  # fmt: skip

    def _write_file(self, rel: str, data: bytes) -> None:
        path = self.media.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # -- checking -----------------------------------------------------------

    async def check(
        self, pkg: ProductionPackage, shot: Shot, take: ShotTake, *, recheck: bool = False
    ) -> None:
        """Check the finished draws that have no check yet (`recheck`: every finished draw),
        all at once. A check that can't run leaves the draw unchecked, with the reason; it
        never counts as a pass."""
        context = shot_context(pkg, shot) | {
            "sub_shot": sub_shot_context(
                pkg, shot, take_sub(pkg, shot, take), self._of(take), take_states(take)
            ),
            "pictures": [
                {"picture": f"<Picture {n}>", "role": r.role}
                for n, r in enumerate(take.references, 1)
            ],
            "prompt": take.prompt,
            "direction": take.direction,
        }
        refs = self._media(take)
        todo = [d for d in take.results
                if d.status == "done" and d.file and (recheck or d.check is None)]  # fmt: skip
        await asyncio.gather(*(self._check_draw(take, d, context, refs) for d in todo))
        self.repo.save_shot_take(take)

    async def _check_draw(
        self, take: ShotTake, d: FrameDraw, context: dict[str, Any], refs: list[MediaPart]
    ) -> None:
        rel = d.view or d.thumb or d.file or ""
        frame = MediaPart(label="<Frame>", path=self.media.path(rel),
                          mime="image/jpeg" if rel.endswith(".jpg") else d.content_type)  # fmt: skip
        request = GenerationRequest(
            stage=CHECK_STAGE,  # type: ignore[arg-type]
            instructions=judge_instructions(),
            context=context,
            media=[*refs, frame],
        )
        try:
            out = await self.text.generate(request, JudgeOutput)
        except Exception as exc:
            log.warning("shot check failed for %s/%d", take.take_id, d.n, exc_info=True)
            d.check, d.check_error = None, f"The check couldn't run: {str(exc)[:200]}"
            return
        total, ok = score(out.items)
        d.check = FrameCheck(items=out.items, summary=out.summary.strip(),
                             fix=out.fix.strip(), score=total, passed=ok,
                             model=request.model or getattr(self.text, "model_id", None))  # fmt: skip
        d.check_error = None

    def start_check(self, take_id: str, *, recheck: bool) -> None:
        """Check a finished take's draws in the background ("Check again"); the API has
        set its status to "checking"."""
        task = asyncio.create_task(self._check_again(take_id, recheck))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _check_again(self, take_id: str, recheck: bool) -> None:
        take = self.repo.get_shot_take(take_id)
        if take is None:
            return
        try:
            pkg = self.repo.get_package(take.package_id, take.revision_id)
            if pkg is not None:
                await self.check(pkg, shot_of(pkg, take.shot_id), take, recheck=recheck)
        except Exception:
            log.warning("check again failed for %s", take_id, exc_info=True)
        finally:
            take.status = "done"
            self.repo.save_shot_take(take)


def judge_view(media: MediaStore, rel: str) -> str | None:
    """A JPEG copy with a JUDGE_PX long edge, for the judge (the 512 px thumbnail is too
    small to judge a face; the 16 MP original too big to send). None without ffmpeg."""
    return make_thumb(media, rel, px=JUDGE_PX, suffix="_view", quality=3)
