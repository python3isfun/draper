"""Approvals: which take is busy or approved, and how approving one changes the others."""

from __future__ import annotations

from ..models import ProductionPackage
from ..models.storyboard import Shot
from .models import ShotPlan, ShotTake
from .plans import current_sub


def is_busy(takes: list[ShotTake], shot_id: str, sub_shot: int | None = None) -> bool:
    """Whether a take of the shot (of that sub-shot, if given) is being made."""
    return any(
        t.busy for t in takes
        if t.shot_id == shot_id and (sub_shot is None or t.sub_shot == sub_shot)
    )  # fmt: skip


def approved_take(takes: list[ShotTake], shot_id: str, sub_shot: int) -> ShotTake | None:
    return next(
        (t for t in takes
         if t.shot_id == shot_id and t.sub_shot == sub_shot and t.approved is not None),
        None,
    )  # fmt: skip


def frame_ref(take: ShotTake) -> str:
    """How a later sub-shot's take cites this take's approved draw."""
    return f"frame:{take.take_id}:{take.approved}"


def approval_changes(take: ShotTake, n: int | None, others: list[ShotTake]) -> list[ShotTake]:
    """The takes whose `approved` changes when draw `n` of `take` is approved (None:
    withdrawn); `others` are the shot's other takes. One approved draw per sub-shot, and a
    later sub-shot's approved keyframe must be drawn from the approved keyframe before it:
    changing or withdrawing one withdraws those drawn from another, down the chain. A take
    kept across an inserted cut (`moved_by_insert`) is drawn from the cut before the insert:
    it stays approved, out of date (staleness.py), while that keyframe stays approved."""
    changed: list[ShotTake] = []

    def clear(t: ShotTake) -> None:
        t.approved = None
        if all(c is not t for c in changed):
            changed.append(t)

    if take.approved != n:
        take.approved = n
        changed.append(take)
    if n is not None:
        for t in others:
            if t.sub_shot == take.sub_shot and t.approved is not None:
                clear(t)
    every = [take, *others]
    for k in sorted({t.sub_shot for t in every if t.sub_shot > 1}):
        prev = next((t for t in every if t.sub_shot == k - 1 and t.approved is not None), None)
        want = frame_ref(prev) if prev is not None else None
        # A keyframe moved by an inserted cut holds while it is drawn from an approved
        # keyframe of an earlier cut (the cut it continued before the insert).
        earlier = {frame_ref(t) for t in every if t.sub_shot < k - 1 and t.approved is not None}
        for t in every:
            drawn_from = t.references[0].ref_id if t.references else None
            if (
                t.sub_shot == k
                and t.approved is not None
                and drawn_from != want
                and not (t.moved_by_insert and drawn_from in earlier)
            ):
                clear(t)
    return changed


def stale_approvals(
    pkg: ProductionPackage, shot: Shot, takes: list[ShotTake], plan: ShotPlan
) -> list[ShotTake]:
    """The shot's approved takes the plan no longer matches: their sub-shot is gone or
    was changed since they were drawn. Withdrawing them withdraws the later ones too."""
    return [
        t for t in takes
        if t.shot_id == shot.shot_id and t.approved is not None and t.sub_shot >= 1
        and current_sub(pkg, shot, plan, t) is None
    ]  # fmt: skip
