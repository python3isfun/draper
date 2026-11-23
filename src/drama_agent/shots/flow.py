"""What follows from a step of the keyframe work, shared by the API and the episode run:
starting a take, approving a draw (and starting the next cut from it), and writing a
shot's video prompt once every cut has its keyframe.

The API endpoints (api/shot_frames.py) and the episode runner (shots/episode.py) do the
same things when a person clicks and when the run decides, so the rules live here once.
Refusals are ValueError with the sentence the API shows (it maps them to 409).
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..models import ProductionPackage
from ..models.storyboard import Shot
from ..shot_reading import SceneReading
from .approvals import approved_take, frame_ref, is_busy
from .common import NotReady, shot_of
from .models import ShotPlan, ShotTake, ShotVideoPrompt
from .plans import current_sub, plan_of
from .takes import plan_take
from .video_prompt import approved_frames, fingerprint, prompt_state


def start_take(repo: Any, runner: Any, take: ShotTake) -> ShotTake:
    """Save a planned take and run it; refused while its shot is being planned (an insert
    moves its takes when written)."""
    stored = repo.get_shot_plan(take.package_id, take.revision_id, take.shot_id)
    if stored is not None and stored.busy:
        raise ValueError(f"{take.shot_id} is being planned; draw once it is done")
    repo.save_shot_take(take)
    runner.start(take.take_id)
    return take


def drawn_from(
    repo: Any, pkg: ProductionPackage, shot: Shot, plan: ShotPlan, index: int, frame: str
) -> bool:
    """Whether sub-shot `index` has a take (for the plan as it is) drawn from `frame`, or
    one still being made."""
    takes = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
    if is_busy(takes, shot.shot_id, index):
        return True
    return any(
        t.shot_id == shot.shot_id and t.sub_shot == index
        and current_sub(pkg, shot, plan, t) is not None
        and t.references and t.references[0].ref_id == frame
        for t in takes
    )  # fmt: skip


def start_prompt(repo: Any, runner: Any, pkg: ProductionPackage, shot_id: str) -> ShotVideoPrompt:
    """Write the shot's video prompt in the background; the prompt it replaces stays shown
    meanwhile."""
    stored = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot_id)
    vp = (stored.model_copy(update={"status": "writing", "error": None}) if stored
          else ShotVideoPrompt(package_id=pkg.package_id, revision_id=pkg.revision_id,
                               shot_id=shot_id))  # fmt: skip
    repo.save_video_prompt(vp)
    runner.start_video_prompt(vp)
    return vp


def auto_prompt(repo: Any, runner: Any, pkg: ProductionPackage, shot: Shot) -> bool:
    """Once every cut of the shot has its approved keyframe, write its video prompt, unless
    the one it has is current or being written. Returns whether one was started."""
    stored = repo.get_video_prompt(pkg.package_id, pkg.revision_id, shot.shot_id)
    if stored is not None and stored.busy:
        return False
    plan = plan_of(pkg, shot, repo.get_shot_plan(pkg.package_id, pkg.revision_id, shot.shot_id))
    frames = approved_frames(pkg, shot, plan, repo.list_shot_takes(pkg.package_id, pkg.revision_id))
    if not frames or not all(frames.values()):
        return False
    context, ids = runner.video_inputs(pkg, shot)
    if prompt_state(stored, fingerprint(context, ids)) == "current":
        return False
    start_prompt(repo, runner, pkg, shot.shot_id)
    return True


def approve_draw(
    repo: Any,
    runner: Any,
    pkg: ProductionPackage,
    take: ShotTake,
    n: int,
    *,
    next_cut: bool = True,
    reading: SceneReading | None = None,
) -> ShotTake:
    """Approve draw `n` of `take` as its cut's keyframe (a plain switch: one approved draw
    per cut). With `next_cut`, the next cut's keyframe starts, drawn from this one with the
    same settings, unless that cut keeps an approved keyframe or already has a take drawn
    from this one. Then the shot's video prompt is written if every cut is approved."""
    d = next((d for d in take.results if d.n == n), None)
    if take.busy or d is None or d.status != "done":
        raise ValueError("only a finished draw of a finished take can be approved")
    shot = shot_of(pkg, take.shot_id)
    stored = repo.get_shot_plan(pkg.package_id, pkg.revision_id, shot.shot_id)
    plan = plan_of(pkg, shot, stored)
    if stored is not None and stored.busy:
        raise ValueError(f"{shot.shot_id} is being planned; approve once it is done")
    if current_sub(pkg, shot, plan, take) is None:
        raise ValueError("the sub-shot plan changed since this take was drawn")
    out = repo.approve_shot_draw(take.take_id, n)
    following = plan.sub(take.sub_shot + 1)
    if (
        next_cut
        and following is not None
        # Unless the next cut keeps an approved keyframe (one kept across an inserted cut
        # is redrawn when the user chooses: it is marked out of date).
        and approved_take(repo.list_shot_takes(pkg.package_id, pkg.revision_id), shot.shot_id,
                          following.index) is None
        # Unless the next cut already has a take drawn from this keyframe.
        and not drawn_from(repo, pkg, shot, plan, following.index, frame_ref(out))
    ):  # fmt: skip
        with contextlib.suppress(NotReady, ValueError):
            start_take(repo, runner, plan_take(
                pkg,
                repo.list_images(pkg.package_id, pkg.revision_id),
                repo.list_shot_takes(pkg.package_id, pkg.revision_id),
                plan,
                sub_shot=following.index,
                draws=take.draws,
                max_rounds=take.max_rounds,
                reading=reading if reading is not None else runner.fresh_reading(pkg, shot.scene_id),
                **runner.take_options(take.provider),  # the same provider
            ))  # fmt: skip
    auto_prompt(repo, runner, pkg, shot)
    return out
