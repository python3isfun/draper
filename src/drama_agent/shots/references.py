"""The pictures a sub-shot's keyframe is drawn from."""

from __future__ import annotations

import os
from typing import Any

from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.storyboard import Shot
from ..shot_reading import SceneReading
from .approvals import approved_take, frame_ref
from .common import _names
from .models import FrameRef, ShotTake, SubShot
from .picks import ShotPicks, shot_picks


def sub_shot_references(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    shot: Shot,
    sub: SubShot,
    picks: ShotPicks | None = None,
    reading: SceneReading | None = None,
    judge: Any = None,
) -> tuple[list[FrameRef], list[str]]:
    """(the pictures a sub-shot's keyframe is drawn from, what is missing): for each person
    in this sub-shot, the picture a fresh `reading` of the scene chose (with their state at
    this moment), else their storyboard look; the set (the picture of the area the reading
    placed this cut in, when the set has one, else its wide view) and the props. First comes the
    approved keyframe of the sub-shot before it (sub-shot 2 on; it must be approved), or for
    sub-shot 1, of the previous shot of the scene when it has one (light, set dressing and
    everyone's state carry over the cut; not waited for). Chosen per sub-shot, then capped at
    nine, so a close-up keeps its person's face however crowded the shot is. With `judge`
    (keyframe_judge), a keyframe that is out of date is never drawn from: the cut before
    must be redrawn first, and the previous shot's is left out until it is."""
    picks = picks or shot_picks(pkg, images, shot)
    people = set(sub.people)
    chosen = chosen_pictures(pkg, shot, sub, reading)
    # A person the reading drew from a picture of theirs needs no storyboard pick.
    missing = picks.missing_for(people - set(chosen))
    previous = previous_shot = None
    if sub.index > 1:
        prev = approved_take(takes, shot.shot_id, sub.index - 1)
        if prev is None:
            missing.append(f"an approved keyframe for sub-shot {sub.index - 1}")
        elif judge is not None and judge(prev):
            missing.append(f"a new keyframe for sub-shot {sub.index - 1} (the approved one "
                           "is out of date)")  # fmt: skip
        else:
            previous = FrameRef(ref_id=frame_ref(prev), kind="previous_frame",
                                role=f"the approved keyframe of sub-shot {sub.index - 1}, "
                                "just before this cut", likeness=prev.likeness)  # fmt: skip
    else:
        previous_shot = previous_shot_frame(pkg, takes, shot)
        if previous_shot is not None and judge is not None:
            before = next((t for t in takes if t.take_id == previous_shot.ref_id.split(":")[1]),
                          None)  # fmt: skip
            if before is not None and judge(before):
                previous_shot = None  # it shows an earlier design
    # The part of the set behind this framing, as the reading chose it (0: the wide view).
    # Off unless DRAMA_KEYFRAME_SET_AREAS=1: tried on Heartbeats episode 1 (2026-11-27), an
    # area picture pulled close shots wider (the person drawn at the empty room's scale)
    # and brought its own foreground into the frame (a fender across a close-up).
    area = 0
    if reading is not None and os.environ.get("DRAMA_KEYFRAME_SET_AREAS") == "1":
        area = reading.set_view(shot.shot_id, sub.index)
    return picks.refs(people, previous, chosen, previous_shot, area), missing


def chosen_pictures(
    pkg: ProductionPackage, shot: Shot, sub: SubShot, reading: SceneReading | None
) -> dict[str, FrameRef]:
    """Each person's picture as a fresh reading chose it for this sub-shot, with their
    state; empty without a reading."""
    if reading is None:
        return {}
    names = _names(pkg)
    pool = {c.image_id: c for c in reading.candidates}
    out: dict[str, FrameRef] = {}
    for who in sub.people:
        c = reading.choice(shot.shot_id, sub.index, who)
        cand = c and pool.get(c.image_id)
        if c is None or cand is None:
            continue
        out[who] = FrameRef(ref_id=c.image_id, kind=cand.kind, entity_id=who,
                            likeness=cand.likeness, state=c.state, keep=c.keep,
                            role=f"{names.get(who, who)}, the design picture closest to this "
                            f"moment ({cand.label[:80]})")  # fmt: skip
    return out


def previous_shot_frame(
    pkg: ProductionPackage, takes: list[ShotTake], shot: Shot
) -> FrameRef | None:
    """The approved keyframe that last opened a cut of the shot before this one in its
    scene (its highest approved sub-shot), if any."""
    before = [s for s in pkg.shot_list
              if s.scene_id == shot.scene_id and s.start_ms < shot.start_ms]  # fmt: skip
    if not before:
        return None
    prev_shot = max(before, key=lambda s: s.start_ms)
    approved = [t for t in takes if t.shot_id == prev_shot.shot_id and t.approved is not None
                and t.sub_shot >= 1]  # fmt: skip
    if not approved:
        return None
    last = max(approved, key=lambda t: t.sub_shot)
    return FrameRef(ref_id=frame_ref(last), kind="previous_shot", likeness=last.likeness,
                    role=f"the approved keyframe of the previous shot ({prev_shot.shot_id}, "
                    f"[Shot {last.sub_shot}]): the set and light a moment earlier")  # fmt: skip
