"""Planning a take: its sub-shot, pictures, seeds and version; the next automatic round."""

from __future__ import annotations

import random
import uuid

from ..imagegen import MAX_REFERENCES
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.package import utcnow
from ..shot_reading import SceneReading
from .approvals import approved_take, frame_ref, is_busy
from .common import NotReady, frame_size, shot_of
from .models import (
    DEFAULT_DRAWS,
    DEFAULT_PROVIDER,
    MAX_DRAWS,
    MAX_ROUNDS,
    FrameRef,
    PromptVersion,
    ShotPlan,
    ShotTake,
    SubShot,
)
from .plans import current_sub
from .references import previous_shot_frame, sub_shot_references
from .staleness import design_print, keyframe_judge


def _seeds(seeds: list[int] | None, n: int) -> list[int]:
    out = list(dict.fromkeys(seeds or []))[:n]
    return out + [random.randint(1, 2**31 - 1) for _ in range(n - len(out))]


def sub_shot_for(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plan: ShotPlan,
    sub_shot: int,
    reading: SceneReading | None = None,
) -> tuple[SubShot, list[FrameRef]]:
    """(the sub-shot, the pictures its keyframe is drawn from), or NotReady saying why it
    can't be drawn now. With a fresh `reading` of the scene, each person's picture is the one
    it chose."""
    shot = shot_of(pkg, plan.shot_id)
    if plan.busy:
        raise NotReady(f"the sub-shots of {shot.shot_id} are still being planned")
    sub = plan.sub(sub_shot)
    if sub is None:
        raise NotReady(f"{shot.shot_id} has no sub-shot {sub_shot}")
    refs, missing = sub_shot_references(pkg, images, takes, shot, sub, reading=reading,
                                        judge=keyframe_judge(pkg, images, takes))  # fmt: skip
    if missing:
        raise NotReady("first: " + "; ".join(missing))
    if not refs:
        raise NotReady(f"{shot.shot_id} has no design images to draw from")
    return sub, refs


def plan_take(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plan: ShotPlan,
    *,
    sub_shot: int = 1,
    draws: int = DEFAULT_DRAWS,
    seeds: list[int] | None = None,
    direction: str | None = None,
    version: PromptVersion | None = None,
    auto: bool = True,
    max_rounds: int = MAX_ROUNDS,
    reading: SceneReading | None = None,
    provider: str = DEFAULT_PROVIDER,
    max_references: int = MAX_REFERENCES,
) -> ShotTake:
    """A new take on a sub-shot's keyframe: written by the model (steered by `direction`,
    and building on the newest take's prompt and check), or drawn from a prompt `version`
    as it is. Without `seeds`, a version is drawn with the newest take's seeds, so the two
    compare draw for draw. `provider` draws it, taking at most `max_references` pictures:
    a written take drops the last (the faces come last); a version citing more is refused."""
    shot = shot_of(pkg, plan.shot_id)
    shot_id = shot.shot_id
    if is_busy(takes, shot_id, sub_shot):
        raise NotReady(f"sub-shot {sub_shot} of {shot_id} is still being made")
    sub, refs = sub_shot_for(pkg, images, takes, plan, sub_shot, reading)
    if version is not None and version.references:
        # The pictures its numbers were written for, with any earlier keyframe swapped for
        # the one approved now (same slot, so the numbers still hold).
        refs = []
        done = {i.image_id for i in images if i.status == "done" and i.file}
        for r in version.references:
            if r.kind in ("previous_frame", "start_frame"):
                prev = approved_take(takes, shot_id, sub_shot - 1)
                if prev is None:
                    raise NotReady(f"approve a keyframe for sub-shot {sub_shot - 1} first: "
                                   f"prompt v{version.v} is drawn from it")  # fmt: skip
                r = r.model_copy(update={"ref_id": frame_ref(prev), "likeness": prev.likeness})
            elif r.kind == "previous_shot":
                now = previous_shot_frame(pkg, takes, shot)
                if now is None:
                    raise NotReady(f"prompt v{version.v} was written with the previous shot's "
                                   "keyframe, which isn't approved now")  # fmt: skip
                r = r.model_copy(update={"ref_id": now.ref_id, "likeness": now.likeness})
            elif r.ref_id not in done:
                raise NotReady(f"prompt v{version.v} names a picture that is gone ({r.role})")
            refs.append(r)
    if len(refs) > max_references:
        if version is not None:
            raise NotReady(f"prompt v{version.v} cites {len(refs)} pictures; {provider} takes "
                           f"{max_references}")  # fmt: skip
        refs = refs[:max_references]
    if version is not None and (version.sub_shot, version.spec) != (sub_shot, sub):
        raise NotReady(f"prompt v{version.v} was written for an older plan of this sub-shot")
    draws = max(1, min(draws, MAX_DRAWS))
    mine = sorted((t for t in takes if t.shot_id == shot_id and t.sub_shot == sub_shot
                   and current_sub(pkg, shot, plan, t) is not None),
                  key=lambda t: t.created_at)  # fmt: skip
    last = mine[-1] if mine else None
    if version is not None and seeds is None and last is not None:
        seeds = last.seeds
    fixes = _fixes(last) if last is not None and direction is None and version is None else []
    take_id = f"stk_{uuid.uuid4().hex[:12]}"
    return ShotTake(
        take_id=take_id,
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        shot_id=shot_id,
        sub_shot=sub_shot,
        spec=sub,
        run_id=take_id,
        prompt=version.prompt if version else "",
        prompt_source=version.source if version else "pending",
        prompt_model=version.model if version else None,
        prompt_version=version.v if version else None,
        warnings=list(version.findings) if version else [],
        references=refs,
        size=frame_size(pkg),
        provider=provider,
        design_print=design_print(pkg, shot),
        draws=draws,
        seeds=_seeds(seeds, draws),
        direction=(direction or "").strip() or None,
        previous_prompt=last.prompt if last is not None and last.prompt else None,
        fixes=fixes,
        # A chosen version is drawn as it is: no automatic rewrite of it.
        auto=auto and version is None,
        max_rounds=max(1, min(max_rounds, MAX_ROUNDS)),
        likeness=any(r.likeness for r in refs),
    )


def _fixes(take: ShotTake) -> list[str]:
    """What the next round must fix: the best draw's failed items and the judge's fix."""
    best = take.best()
    if best is None or best.check is None:
        return []
    out = [f"{i.item}: {i.finding}" for i in best.check.items if not i.passed and i.finding]
    if best.check.fix:
        out.append(f"Suggested fix: {best.check.fix}")
    return out


def next_round(take: ShotTake) -> ShotTake | None:
    """The next automatic round of a take whose draws all failed the check, if any is
    left: the prompt rewritten against the findings, drawn with the same seeds."""
    if not take.auto or take.round >= take.max_rounds or take.passed() or take.refused:
        return None
    if take.best() is None:  # nothing was checked: nothing to revise against
        return None
    return take.model_copy(
        update={
            "take_id": f"stk_{uuid.uuid4().hex[:12]}",
            "round": take.round + 1,
            "status": "pending",
            "prompt": "",
            "prompt_source": "pending",
            "prompt_model": None,
            "prompt_version": None,
            "warnings": [],
            "notes": "",
            "results": [],
            "previous_prompt": take.prompt,
            "fixes": _fixes(take),
            "direction": take.direction,
            "approved": None,
            "error": None,
            "created_at": utcnow(),
            "finished_at": None,
        },
        deep=True,
    )
