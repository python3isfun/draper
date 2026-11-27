"""A shot's design picks (looks, faces, set, props) as reference pictures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..images import selected_image
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.storyboard import Shot
from .common import _cached, _names, keyframe_max_references
from .models import SHOT_VIEWS, FrameRef


@dataclass
class ShotPicks:
    """A shot's design picks as reference pictures, before they are chosen per sub-shot."""

    looks: list[FrameRef]  # per person: their look, or their face when they have no look
    faces: list[FrameRef]  # the faces of people who also have a look
    sets: list[FrameRef]
    props: list[FrameRef]
    missing: list[tuple[str | None, str]]  # (the character it is for, or None; what)

    def refs(
        self,
        people: set[str] | None = None,
        previous: FrameRef | None = None,
        chosen: dict[str, FrameRef] | None = None,
        previous_shot: FrameRef | None = None,
    ) -> list[FrameRef]:
        """The pictures for `people` (None: everyone), in the order the prompt names them,
        as dramer sends them: the previous keyframe, each person in their look (or the
        picture `chosen` for them by reading the scene), the set's wide view, the props, then
        faces while the limit allows (`keyframe_max_references`: six unless set)."""
        chosen = chosen or {}

        def mine(r: FrameRef) -> bool:
            return people is None or r.entity_id in people

        looks = [chosen.get(r.entity_id or "", r) for r in self.looks if mine(r)]
        looks += [r for who, r in chosen.items() if (people is None or who in people)
                  and all(x.entity_id != who for x in looks)]  # fmt: skip
        used = {r.ref_id for r in looks}
        faces = [f for f in self.faces if mine(f) and f.ref_id not in used]
        refs = [*([previous] if previous else []), *looks, *self.sets,
                *([previous_shot] if previous_shot else []), *self.props]  # fmt: skip
        most = keyframe_max_references()
        refs = refs[:most]
        return refs + faces[: most - len(refs)]

    def missing_for(self, people: set[str] | None = None) -> list[str]:
        """What is missing for `people` (None: everyone); the set and props always count."""
        return [m for ch, m in self.missing if ch is None or people is None or ch in people]


def shot_picks(pkg: ProductionPackage, images: list[GeneratedImage], shot: Shot) -> ShotPicks:
    names = _names(pkg)
    reqs = _cached(pkg, "requirements",
                   lambda: {r.requirement_id: r for r in pkg.asset_requirements.items})  # fmt: skip
    wanted = [reqs[r] for r in shot.asset_requirement_ids if r in reqs]
    missing: list[tuple[str | None, str]] = []

    def pick(req: Any, ch: str | None = None) -> GeneratedImage | None:
        view = SHOT_VIEWS.get(req.asset_type)
        d = next((d for d in req.deliverables if d.view == view), None)
        if d is None:
            return None
        img = selected_image(images, (req.requirement_id, d.deliverable_id))
        if img is None:
            missing.append((ch, f"{_req_title(req, names)} ({view})"))
        return img

    def of(kind: str, entity: str) -> Any:
        return next(
            (r for r in wanted if r.asset_type == kind and r.subject_refs
             and r.subject_refs[0].entity_id == entity),
            None,
        )  # fmt: skip

    looks: list[FrameRef] = []
    faces: list[FrameRef] = []
    for ch in shot.character_ids:
        name = names.get(ch, ch)
        look_req, face_req = of("character_look", ch), of("character_identity", ch)
        look = pick(look_req, ch) if look_req else None
        face = pick(face_req, ch) if face_req else None
        if look is not None:
            looks.append(FrameRef(ref_id=look.image_id, role=f"{name} in this shot's look, "
                                  "full body", kind="look", entity_id=ch,
                                  likeness=look.likeness))  # fmt: skip
        if face is not None:
            ref = FrameRef(ref_id=face.image_id, role=f"{name}'s face", kind="face",
                           entity_id=ch, likeness=face.likeness)  # fmt: skip
            (faces if look is not None else looks).append(ref)
        if look is None and face is None and not (look_req or face_req):
            missing.append((ch, f"{name} (no design image is planned)"))
    sets: list[FrameRef] = []
    loc = next((r for r in wanted if r.asset_type == "location"), None)
    if loc is not None and (img := pick(loc)) is not None:
        sets.append(FrameRef(ref_id=img.image_id, role=f"{names.get(shot.location_id, '')} "
                             "(the set), wide view", kind="set", entity_id=shot.location_id))  # fmt: skip
    props: list[FrameRef] = []
    for r in shot.prop_state_refs:
        req = of("prop", r.prop_id)
        if req is not None and (img := pick(req)) is not None:
            props.append(FrameRef(ref_id=img.image_id, role=f"{names.get(r.prop_id, r.prop_id)} "
                                  "(prop)", kind="prop", entity_id=r.prop_id))  # fmt: skip
    return ShotPicks(looks, faces, sets, props, missing)


def shot_references(
    pkg: ProductionPackage, images: list[GeneratedImage], shot: Shot
) -> tuple[list[FrameRef], list[str]]:
    """(the pictures for the whole shot, what is missing); see ShotPicks.refs."""
    picks = shot_picks(pkg, images, shot)
    return picks.refs(), picks.missing_for()


def _req_title(req: Any, names: dict[str, str]) -> str:
    entity = req.subject_refs[0].entity_id if req.subject_refs else req.requirement_id
    return names.get(entity, entity)
