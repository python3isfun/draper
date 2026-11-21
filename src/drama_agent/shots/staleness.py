"""What is out of date after a design change: keyframes, plans, and the design text they
were made against."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.storyboard import Shot
from .approvals import approved_take, frame_ref
from .models import FrameRef, ShotPlan, ShotTake
from .plans import current_sub, plan_of
from .references import previous_shot_frame

# Why an approved keyframe is out of date.
KF_TEXT_CHANGED = "the set, look or prop text changed since it was drawn"

KF_BEFORE_CHANGED = "the keyframe it continues from has changed"

KF_SHOT_REVISED = "a revision changed the shot since it was drawn"

KF_BEFORE_OUTDATED = "the keyframe it continues from is out of date"  # redraw that one first

KF_CUT_INSERTED = "a cut was inserted before it: redraw it from the new cut's keyframe"


def design_print(pkg: ProductionPackage, shot: Shot) -> str:
    """A fingerprint of the design text a keyframe of this shot is written and checked
    against: its people's cast entries and looks, its set and state, its props and states
    (the sheets and the story bible's one-line descriptions)."""
    entries: dict[str, Any] = {}
    for sheet in (pkg.cast_sheet, pkg.set_sheet, pkg.prop_sheet):
        if sheet is not None:
            entries |= sheet.entries()
    bible = pkg.story_bible
    ids = design_ids(shot)
    lines = {x.look_id: x.description for x in bible.looks}
    lines |= {x.location_state_id: x.description for x in bible.location_states}
    lines |= {x.prop_state_id: x.description for x in bible.prop_states}
    data = {i: [e.model_dump() if (e := entries.get(i)) is not None else None, lines.get(i)]
            for i in ids}  # fmt: skip
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def design_ids(shot: Shot) -> list[str]:
    """The cast, set and prop entries a shot is designed from."""
    return [*shot.character_ids, *(r.look_id for r in shot.character_look_refs),
            shot.location_id, shot.location_state_id,
            *(x for r in shot.prop_state_refs for x in (r.prop_id, r.prop_state_id))]  # fmt: skip


def edited_since(pkg: ProductionPackage, shot: Shot, when: datetime) -> bool:
    """Whether a hand edit (sheet_edit) to the shot's set, looks or props was made after
    `when`. An edit from before edits recorded their time counts as after: this is only
    asked of work that predates design prints too."""
    ids = set(design_ids(shot))
    return any(
        e.note.startswith("Edited by hand") and ids & set(e.target_ids)
        and (e.at is None or e.at > when)
        for e in pkg.change_log
    )  # fmt: skip


def revised_since(pkg: ProductionPackage, shot_id: str, when: datetime) -> bool:
    """Whether a revise run (pipeline/revise.py) changed the shot after `when`."""
    return any(
        e.note.startswith("Revised") and shot_id in e.changed_ids
        and e.at is not None and e.at > when
        for e in pkg.change_log
    )  # fmt: skip


PLAN_SHOT_REVISED = "before a revision changed the shot: plan the cuts again"

PLAN_TEXT_CHANGED = (
    "before the set, look or prop text changed: its framing may describe the old design; "
    "plan the cuts again"
)


def plan_outdated(pkg: ProductionPackage, shot: Shot, plan: ShotPlan | None) -> str | None:
    """Why a stored cut plan may describe an earlier design, or None."""
    if plan is None or plan.source == "single" or plan.status != "done":
        return None
    if revised_since(pkg, shot.shot_id, plan.updated_at):
        return PLAN_SHOT_REVISED
    if plan.design_print is not None:
        return PLAN_TEXT_CHANGED if plan.design_print != design_print(pkg, shot) else None
    return PLAN_TEXT_CHANGED if edited_since(pkg, shot, plan.updated_at) else None


def keyframe_judge(
    pkg: ProductionPackage, images: list[GeneratedImage], takes: list[ShotTake]
) -> Any:
    """A function telling why a keyframe take is out of date (empty when it isn't): the
    design text it was written against changed, a design picture it was drawn from was
    replaced or is out of date (see images.outdated_pictures), or the keyframe it continues
    from (the cut before, or the previous shot's) changed or is out of date itself."""
    from ..images import PICTURE_OUTDATED, PICTURE_REPLACED, is_current, outdated_pictures

    shots = {s.shot_id: s for s in pkg.shot_list}
    stale_pictures = outdated_pictures(pkg, images)
    prints: dict[str, str] = {}
    memo: dict[str, list[str]] = {}

    def reasons(t: ShotTake) -> list[str]:
        if t.take_id in memo:
            return memo[t.take_id]
        memo[t.take_id] = []  # a cycle can't happen, but never loop
        shot = shots[t.shot_id]
        out: list[str] = []
        if t.design_print is not None:
            if t.shot_id not in prints:
                prints[t.shot_id] = design_print(pkg, shot)
            if t.design_print != prints[t.shot_id]:
                out.append(KF_TEXT_CHANGED)
        elif edited_since(pkg, shot, t.created_at):
            out.append(KF_TEXT_CHANGED)
        if revised_since(pkg, t.shot_id, t.created_at):
            out.append(KF_SHOT_REVISED)
        for r in t.references:
            if r.kind in ("previous_frame", "start_frame", "previous_shot"):
                now = (previous_shot_frame(pkg, takes, shot) if r.kind == "previous_shot"
                       else (p := approved_take(takes, t.shot_id, t.sub_shot - 1))
                       and FrameRef(ref_id=frame_ref(p), role="", kind="previous_frame"))  # fmt: skip
                if now is None or now.ref_id != r.ref_id:
                    inserted = t.moved_by_insert and r.kind != "previous_shot"
                    out.append(KF_CUT_INSERTED if inserted else KF_BEFORE_CHANGED)
                    continue
                before_id = r.ref_id.split(":")[1]
                before = next((x for x in takes if x.take_id == before_id), None)
                if before is not None and reasons(before):
                    out.append(KF_BEFORE_OUTDATED)
            elif r.ref_id.startswith("img_"):
                if not is_current(images, r.ref_id):
                    out.append(f"{PICTURE_REPLACED}: {r.role}")
                elif r.ref_id in stale_pictures:
                    out.append(f"{PICTURE_OUTDATED}: {r.role}")
        memo[t.take_id] = list(dict.fromkeys(out))
        return memo[t.take_id]

    return reasons


def outdated_keyframes(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plans: list[ShotPlan],
    judge: Any = None,
) -> dict[str, list[str]]:
    """Each approved keyframe (of the plans as they are) that is out of date, by take id,
    with why (keyframe_judge)."""
    reasons = judge or keyframe_judge(pkg, images, takes)
    stored = {p.shot_id: p for p in plans}
    shots = {s.shot_id: s for s in pkg.shot_list}
    out: dict[str, list[str]] = {}
    for t in takes:
        if t.approved is None or t.sub_shot < 1 or t.shot_id not in shots:
            continue
        shot = shots[t.shot_id]
        if current_sub(pkg, shot, plan_of(pkg, shot, stored.get(t.shot_id)), t) is None:
            continue  # stale_approvals' business: its sub-shot changed
        if why := reasons(t):
            out[t.take_id] = why
    return out
