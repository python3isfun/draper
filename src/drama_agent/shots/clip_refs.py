"""The pictures video production sends with a shot's clip, in priority order: one list for
the video prompt writer (video_prompt.py) and the export (shot_specs.py), so the prompt
names exactly the pictures the clip is sent with.

Keyframes come first (each cut's approved keyframe, in cut order), then the faces (to hold
each likeness), as video production asks: a consumer with a smaller picture limit drops
from the end. A shot whose every cut has a keyframe then sends the props (seen closer),
each person's design picture and the set; any other shot sends each person's picture, the
set and the props, then any further pictures of a person. At most VIDEO_MAX_REFERENCES are
sent; the rest are left out.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..design.slots import key_of, selected_image
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.storyboard import Shot
from ..shot_reading import PersonState, SceneReading
from .models import FrameRef, ShotPlan
from .picks import shot_picks
from .references import chosen_pictures

# The most pictures video production sends with one clip (dramer cuts H3's to 9).
VIDEO_MAX_REFERENCES = 9


def state_note(st: PersonState) -> str:
    """A person's state as one labelled line ("Wearing: a robe. Holding: one shoe."); a
    blank field is left out, so nothing is said about hands the reading left blank."""
    parts = [("Wearing", st.wearing), ("Hair", st.hair), ("Holding", st.carrying),
             ("", st.condition)]  # fmt: skip
    out = []
    for label, v in parts:
        v = (v or "").strip().rstrip(".; ")
        if v:
            out.append(f"{label}: {v}." if label else f"{v}.")
    return " ".join(out)


def exportable(images: list[GeneratedImage]) -> set[str]:
    """The design pictures an export carries: each deliverable's selected finished picture
    (else its newest) with a file."""
    keys = {key_of(i) for i in images}
    return {img.image_id for k in keys if (img := selected_image(images, k)) and img.file}


def clip_references(
    pkg: ProductionPackage,
    shot: Shot,
    plan: ShotPlan,
    frames: dict[int, str | None],
    reading: SceneReading | None,
    images: list[GeneratedImage],
    usable: Callable[[str], bool],
    *,
    limit: int = VIDEO_MAX_REFERENCES,
    anchors_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """(the pictures sent, the pictures left out, each person's state note). `frames` maps
    each cut to its approved keyframe (frame:<take>:<n>) where the shot's keyframes count;
    `usable` says whether a design picture can be sent (it is in the export). Each picture
    is {ref_id, kind, entity_id, sub_shots, likeness, about}. `limit` caps how many are
    sent (default VIDEO_MAX_REFERENCES); `anchors_only` sends just the identity anchors:
    every cut's keyframe and the face of each person on screen (the fast option; a cut
    without a keyframe or a person without a face picture otherwise drifts to another
    face), falling back to the usual priority order for a shot that has no keyframe yet."""
    all_frames = bool(frames) and all(frames.values())
    chosen = {sub.index: chosen_pictures(pkg, shot, sub, reading) for sub in plan.sub_shots}

    def ref(r: FrameRef, kind: str, cuts: list[int] | None = None) -> dict[str, Any] | None:
        if not usable(r.ref_id):
            return None
        return {"ref_id": r.ref_id, "kind": kind, "entity_id": r.entity_id,
                "sub_shots": cuts or [], "likeness": r.likeness, "about": r.role}  # fmt: skip

    refs: list[dict[str, Any]] = [
        {"ref_id": f, "kind": "keyframe", "entity_id": None, "sub_shots": [i],
         "likeness": False, "about": f"approved keyframe of [Shot {i}]"}
        for i, f in frames.items() if f
    ]  # fmt: skip

    # Each person's pictures: those the reading chose for their cuts (the first in their
    # look's place), else the storyboard's look.
    picks = shot_picks(pkg, images, shot)
    firsts: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    person_pics: set[str] = set()
    notes: dict[str, str] = {}
    for who in shot.character_ids:
        mine: dict[str, tuple[FrameRef, list[int]]] = {}
        for i, by_who in chosen.items():
            if (r := by_who.get(who)) is not None:
                mine.setdefault(r.ref_id, (r, []))[1].append(i)
                if who not in notes and r.state is not None:
                    notes[who] = state_note(r.state)
        pics = [p for r, cuts in mine.values() if (p := ref(r, "chosen", cuts))]
        if not pics:
            look = next((r for r in picks.looks if r.entity_id == who), None)
            pics = [p] if look and (p := ref(look, look.kind)) else []
        if pics:
            firsts.append(pics[0])
            later.extend(pics[1:])
            person_pics.update(p["ref_id"] for p in pics)
    faces = [p for r in picks.faces if (p := ref(r, "face")) and p["ref_id"] not in person_pics]
    sets = [p for r in picks.sets if (p := ref(r, "set"))]
    props = [p for r in picks.props if (p := ref(r, "prop"))]
    if all_frames:
        rest = [*faces, *props, *firsts, *later, *sets]
    else:
        rest = [*faces, *firsts, *sets, *props, *later]
    if anchors_only and refs:
        # The face of each person on screen in some cut (a person who only speaks off
        # frame needs none); a person without a look has their face among `firsts`.
        on_screen = {who for sub in plan.sub_shots for who in sub.people}
        rest = [p for p in [*faces, *firsts]
                if p["kind"] == "face" and p["entity_id"] in on_screen]  # fmt: skip
    seen = {r["ref_id"] for r in refs}
    for r in rest:
        if r["ref_id"] not in seen:
            seen.add(r["ref_id"])
            refs.append(r)
    cap = max(1, min(limit, VIDEO_MAX_REFERENCES))
    return refs[:cap], refs[cap:], notes
