"""A cut inserted into a shot's plan: the new plan keeps every existing cut's view, and their
takes move to the new numbering, still approved.

The planner writes the whole plan with the new cut (planner.insert_rule); `kept_cuts` holds
each existing cut's view (framing, camera, who is where) to what it was, so its keyframe
still shows it, taking only the new start times and any reworded action. `carry_takes`
then renumbers the shot's takes drawn for the old plan and points them at their cut in the
new one. The cut right after the inserted one was drawn from the cut before the insert:
it is marked `moved_by_insert`, so it stays approved but out of date (staleness.py) until
it is redrawn from the new cut's keyframe. The plan records the cut it was inserted after
(`ShotPlan.inserted_after`), so restoring the plan before it moves those takes back, and
restoring it again moves them as the insert did (`restore_moves`).
"""

from __future__ import annotations

from ..models import ProductionPackage
from ..models.storyboard import Shot
from .models import ShotPlan, ShotTake, SubShot
from .plans import current_sub

# What a cut's keyframe shows: kept as it was when a cut is inserted.
VIEW_FIELDS = ("framing", "camera", "people", "placement", "off_screen")


def new_index(index: int, after: int) -> int:
    """A cut's number once a cut is inserted after cut `after`."""
    return index if index <= after else index + 1


def kept_cuts(old: list[SubShot], new: list[SubShot], after: int) -> list[SubShot]:
    """The planner's `new` cuts with each existing cut's view put back as it was in `old`.
    Raises ValueError unless `new` is `old` plus exactly one cut, right after cut `after`."""
    if len(new) != len(old) + 1:
        raise ValueError(
            f"the plan has {len(new)} cuts, not {len(old) + 1} (one inserted after cut {after})"
        )
    out = list(new)
    for o in old:
        k = new_index(o.index, after) - 1
        out[k] = new[k].model_copy(update={f: getattr(o, f) for f in VIEW_FIELDS})
    if out[after].model_dump(include=set(VIEW_FIELDS)) in [
        out[i].model_dump(include=set(VIEW_FIELDS)) for i in (after - 1, after + 1) if i < len(out)
    ]:
        raise ValueError(f"the new cut repeats the view of a cut next to it (after cut {after})")
    return out


def carry_takes(
    pkg: ProductionPackage,
    shot: Shot,
    old: ShotPlan,
    new: ShotPlan,
    after: int,
    takes: list[ShotTake],
) -> list[ShotTake]:
    """The shot's takes drawn for the `old` plan, moved to their cut of the `new` one (its
    number and spec); approvals are kept. Returns the takes changed (to save)."""
    moved = []
    for t in takes:
        if t.shot_id != shot.shot_id or t.sub_shot < 1 or current_sub(pkg, shot, old, t) is None:
            continue
        k = new_index(t.sub_shot, after)
        spec = new.sub(k)
        if spec is None:
            continue
        t.sub_shot, t.spec = k, spec
        if k == after + 2:  # drawn from the cut before the insert (an earlier flag stays)
            t.moved_by_insert = True
        moved.append(t)
    return moved


def inserted_after(old: list[SubShot], new: list[SubShot]) -> int | None:
    """The cut `new` inserted one after (`old` with one cut more, every old view kept), or
    None when `new` isn't `old` with one cut inserted."""
    if len(new) != len(old) + 1:
        return None
    views = [o.model_dump(include=set(VIEW_FIELDS)) for o in old]
    for after in range(1, len(old) + 1):
        rest = new[:after] + new[after + 1 :]
        if [x.model_dump(include=set(VIEW_FIELDS)) for x in rest] == views:
            return after
    return None


def restore_moves(
    pkg: ProductionPackage,
    shot: Shot,
    current: ShotPlan,
    restored: ShotPlan,
    takes: list[ShotTake],
) -> list[ShotTake]:
    """The takes that move when `restored` replaces `current` across an inserted cut
    (returned to save; none otherwise). Undoing an insert (`current` was written by it, from
    `restored`): the takes `carry_takes` moved go back to their cut. Restoring an insert
    again (`restored` was written by it, from `current`): the takes move as they did then."""
    if current.inserted_after is not None and (
        inserted_after(restored.sub_shots, current.sub_shots) == current.inserted_after
    ):
        return _uncarry(pkg, shot, current, restored, current.inserted_after, takes)
    if restored.inserted_after is not None and (
        inserted_after(current.sub_shots, restored.sub_shots) == restored.inserted_after
    ):
        return carry_takes(pkg, shot, current, restored, restored.inserted_after, takes)
    return []


def _uncarry(
    pkg: ProductionPackage,
    shot: Shot,
    current: ShotPlan,
    restored: ShotPlan,
    after: int,
    takes: list[ShotTake],
) -> list[ShotTake]:
    """The takes moved by the insert that wrote `current` (drawn before it), back to their
    cut of `restored`. The inserted cut's takes, and takes drawn from it since, stay: the
    restored plan doesn't match them, so their approvals are withdrawn as stale."""
    moved = []
    for t in takes:
        if t.shot_id != shot.shot_id or t.sub_shot < 1 or t.sub_shot == after + 1:
            continue
        if t.created_at >= current.updated_at or current_sub(pkg, shot, current, t) is None:
            continue
        k = t.sub_shot if t.sub_shot <= after else t.sub_shot - 1
        spec = restored.sub(k)
        if spec is None:
            continue
        t.sub_shot, t.spec = k, spec
        if k == after + 1:
            t.moved_by_insert = False  # back to continuing from the cut it was drawn from
        moved.append(t)
    return moved
