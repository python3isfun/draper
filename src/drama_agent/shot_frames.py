"""Shot keyframes: a checked picture per sub-shot, made from the design picks, for video.

The video model keeps characters consistent and moves them as intended only as well as its
inputs show it. A storyboard shot is one video generation, but a dynamic one holds two or
three cuts inside it (0-2 s a wide, 2-6 s a close-up), which the video prompt marks as
`[Shot 2] At 00:02.000, the camera cuts to ...`. So before a shot goes to video production
(dramer), each shot gets:

1. **A sub-shot plan**: the text model splits the shot into 1-3 sub-shots (H3's rules: cut
   only for new information; timestamps strictly increasing inside the clip), each with its
   start time, framing, camera, action and who is on screen. A person can edit it. Without
   one, the whole shot is one sub-shot.
2. **A keyframe per sub-shot**: the first frame of that sub-shot, an H3 edit that puts its
   people, in their picked looks, into the picked set with its props. Sub-shot 1 is drawn
   from the design picks; each later one also from the approved keyframe of the sub-shot
   before it (`<Picture 1>`), so light, wardrobe state and set dressing carry across the cut.

Each keyframe goes through a quality loop:

1. **Write**: the text model writes the edit prompt from the sub-shot and the reference
   pictures (`<Picture N>`).
2. **Draw**: a few draws with chosen or random seeds.
3. **Check**: a vision judge compares each draw with the reference pictures and the
   sub-shot, item by item (each person's face, hair and wardrobe; the set and props; how
   many people and where; framing; the action; defects; stray text). The score and the
   verdict are computed here from the items, not taken from the judge.
4. **Revise**: if no draw passes, the failed items and the judge's fix go back to the
   writer for the next round, with the same seeds so rounds compare like for like; up to
   `MAX_ROUNDS` rounds, then a person decides.

A person approves one draw per sub-shot; approving one starts the next sub-shot. Only
approved keyframes of the current plan are exported, with the plan's timestamps, so the
video prompt cites each as the first frame of its `[Shot N]`. Keyframes are production
assets, so the restrictive content rules always apply (as on the design board), and a
shot in a scene staged as implied or simulated intimacy is never drawn from a pick that
shows a real person's likeness (content rule 3).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .content_guard import RULES as CONTENT_RULES
from .content_guard import GuardRefused, text_guard, upload_guard
from .experiment import SIZES, cited_beyond
from .imagegen import MAX_REFERENCES, ImageClient, Reference
from .images import make_thumb, package_has_uploads, selected_image
from .llm import GenerationRequest
from .media import MediaPart, MediaStore
from .models import ProductionPackage
from .models.images import GeneratedImage
from .models.package import utcnow
from .models.storyboard import Shot

log = logging.getLogger(__name__)

WRITE_STAGE = "shot_frames"
CHECK_STAGE = "shot_check"
PLAN_STAGE = "shot_plan"
MAX_SUB_SHOTS = 3
MIN_SUB_SHOT_MS = 1000
MAX_ROUNDS = 3  # automatic rounds before a person decides
MAX_DRAWS = 4
DEFAULT_DRAWS = 2
PASS_SCORE = 80
MAJOR_PENALTY = 25
MINOR_PENALTY = 8
JUDGE_PX = 1024  # long edge of the draw the judge sees
NO_OTHER_TEXT = "No other lettering is added."

TakeStatus = Literal["pending", "writing", "drawing", "checking", "done", "failed"]
CheckKind = Literal[
    "identity", "wardrobe", "set", "prop", "count", "placement", "framing", "action",
    "defects", "text", "style",
]  # fmt: skip

# The deliverable a shot sends for each kind of requirement.
SHOT_VIEWS = {
    "character_look": "full body",
    "character_identity": "front face",
    "location": "wide",
    "prop": "detail",
}


# ---------------------------------------------------------------------------
# Models


class FrameRef(BaseModel):
    """One picture a frame is drawn from, as `<Picture N>` in its prompt."""

    ref_id: str  # a design image (img_...) or an approved keyframe (frame:<take>:<n>)
    role: str  # "Lila in this shot's look, full body", "the approved keyframe of sub-shot 1"
    # previous_frame: the approved keyframe of the sub-shot before ("start_frame": older
    # takes' name for it).
    kind: Literal["look", "face", "set", "prop", "previous_frame", "start_frame"]
    entity_id: str | None = None  # the character, location or prop shown
    likeness: bool = False  # shows a real person's likeness (content rule 3)


class SubShot(BaseModel):
    """One cut inside a shot's video generation: `[Shot N]` of the video prompt."""

    index: int = Field(ge=1)  # 1-based, in time order
    start_ms: int = Field(ge=0)  # from the start of the shot (the clip)
    end_ms: int = Field(gt=0)
    framing: str
    camera: str = ""
    action: str
    people: list[str] = Field(default_factory=list)  # character ids on screen

    @property
    def timecode(self) -> str:
        """The video prompt's timestamp for this cut, e.g. "00:02.000"."""
        s = self.start_ms / 1000
        return f"{int(s // 60):02d}:{s % 60:06.3f}"


class ShotPlan(BaseModel):
    """How a shot is cut inside its one video generation. Stored once written (by the
    model or a person); a shot without one is one sub-shot (`single_plan`)."""

    package_id: str
    revision_id: str
    shot_id: str
    status: Literal["writing", "done", "failed"] = "done"
    source: Literal["single", "model", "user"] = "single"
    sub_shots: list[SubShot] = Field(default_factory=list)
    notes: str = ""
    model: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def busy(self) -> bool:
        return self.status == "writing"

    def sub(self, index: int) -> SubShot | None:
        return next((s for s in self.sub_shots if s.index == index), None)


class PromptVersion(BaseModel):
    """One version of a sub-shot keyframe's prompt. The writer's prompts and a person's
    edits are versions; any version can be drawn, compared or restored."""

    version_id: str = Field(default_factory=lambda: f"spv_{uuid.uuid4().hex[:12]}")
    package_id: str
    revision_id: str
    shot_id: str
    sub_shot: int
    spec: SubShot  # the sub-shot as planned when this version was written
    v: int = 0  # 1, 2, ... per shot and sub-shot (set by the store)
    prompt: str
    source: Literal["model", "template", "user"]
    model: str | None = None
    note: str = ""  # the editor's note, or the writer's
    based_on: int | None = None  # the version this one edits
    take_id: str | None = None  # the take whose writer wrote it
    findings: list[str] = Field(default_factory=list)  # where it breaks the H3 image rules
    created_at: datetime = Field(default_factory=utcnow)


class CheckItem(BaseModel):
    item: str = Field(description='What was checked, e.g. "Lila: face" or "people count".')
    kind: CheckKind
    passed: bool
    severity: Literal["minor", "major"] = Field(
        "major",
        description="major: a viewer would notice, or the video would carry it (wrong face, "
        "wrong clothes, a missing or extra person, wrong side, wrong action, a deformed "
        "hand in view); minor: small and easy to live with.",
    )
    finding: str = Field("", description="What is wrong, concretely; empty when it passes.")


class FrameCheck(BaseModel):
    """The judge's verdict on one draw. `score` and `passed` are computed from the items."""

    items: list[CheckItem] = Field(default_factory=list)
    summary: str = ""
    fix: str = ""  # how the prompt should change, for the next round
    score: int = 0
    passed: bool = False
    model: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


class FrameDraw(BaseModel):
    n: int  # 0-based draw number within the take
    seed: int
    status: Literal["done", "failed"] = "done"
    file: str | None = None
    thumb: str | None = None
    view: str | None = None  # the downscaled copy the judge sees
    content_type: str = "image/jpeg"
    model: str = ""
    check: FrameCheck | None = None
    check_error: str | None = None
    error: str | None = None


class ShotTake(BaseModel):
    """One round of one sub-shot's keyframe: a prompt and its draws, each checked."""

    take_id: str
    package_id: str
    revision_id: str
    shot_id: str
    sub_shot: int = 1  # 0: an end frame from before sub-shots (not shown or exported)
    spec: SubShot | None = None  # the sub-shot as planned when this take was made
    round: int = 1
    run_id: str  # the first take of an automatic run; its later rounds share it
    status: TakeStatus = "pending"
    prompt: str = ""
    prompt_source: Literal["pending", "model", "template", "user"] = "pending"
    prompt_model: str | None = None
    notes: str = ""  # the writer's note to the user
    prompt_version: int | None = None  # the prompt version this take draws
    warnings: list[str] = Field(default_factory=list)  # H3 rule findings on its prompt
    references: list[FrameRef] = Field(default_factory=list)
    size: str
    draws: int = DEFAULT_DRAWS
    seeds: list[int] = Field(default_factory=list)
    results: list[FrameDraw] = Field(default_factory=list)
    direction: str | None = None  # the user's steer for this take
    previous_prompt: str | None = None  # the prompt of the round this one revises
    fixes: list[str] = Field(default_factory=list)  # the judge's findings this round fixes
    auto: bool = True  # go on to another round when no draw passes
    max_rounds: int = MAX_ROUNDS
    motion: str = ""  # the writer's note on how the sub-shot moves after this frame
    approved: int | None = None  # the approved draw (one per shot and sub-shot)
    likeness: bool = False
    refused: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_frames(cls, data: Any) -> Any:
        """Takes from before sub-shots had `frame`: a start frame is sub-shot 1, an end
        frame sub-shot 0 (kept, but no longer shown or exported)."""
        if isinstance(data, dict) and "frame" in data and "sub_shot" not in data:
            data = {**data, "sub_shot": 0 if data["frame"] == "end" else 1}
        return data

    @property
    def busy(self) -> bool:
        return self.status not in ("done", "failed")

    def best(self) -> FrameDraw | None:
        """The draw with the best check (passing first), if any was checked."""
        checked = [d for d in self.results if d.status == "done" and d.check is not None]
        return max(checked, key=lambda d: (d.check.passed, d.check.score), default=None)  # type: ignore[union-attr]

    def passed(self) -> bool:
        return any(d.check is not None and d.check.passed for d in self.results)


def score(items: list[CheckItem]) -> tuple[int, bool]:
    """(score out of 100, passed): every failed item costs points; any failed major item
    fails the draw whatever the score."""
    lost = sum(MAJOR_PENALTY if i.severity == "major" else MINOR_PENALTY
               for i in items if not i.passed)  # fmt: skip
    total = max(0, 100 - lost)
    major = any(not i.passed and i.severity == "major" for i in items)
    return total, bool(items) and not major and total >= PASS_SCORE


# ---------------------------------------------------------------------------
# Planning (pure: the API saves what these return)


class NotReady(ValueError):
    """The sub-shot can't be drawn yet (a design pick is missing, or the keyframe of the
    sub-shot before it isn't approved)."""


def _names(pkg: ProductionPackage) -> dict[str, str]:
    names = {c.character_id: c.name for c in pkg.story_bible.characters}
    names |= {loc.location_id: loc.name for loc in pkg.story_bible.locations}
    names |= {p.prop_id: p.name for p in pkg.story_bible.props}
    return names


def shot_of(pkg: ProductionPackage, shot_id: str) -> Shot:
    shot = next((s for s in pkg.shot_list if s.shot_id == shot_id), None)
    if shot is None:
        raise KeyError(f"no shot {shot_id}")
    return shot


def scene_of(pkg: ProductionPackage, shot: Shot) -> Any:
    return next(
        (sc for ep in pkg.episodes for sc in ep.scenes if sc.scene_id == shot.scene_id), None
    )


def intimate_scene(pkg: ProductionPackage, shot: Shot) -> str | None:
    """The scene's intimacy level when it is staged as implied or simulated, else None."""
    sc = scene_of(pkg, shot)
    level = sc.intimacy.level if sc is not None and sc.intimacy is not None else "none"
    return level if level in ("implied", "simulated") else None


def frame_size(pkg: ProductionPackage) -> str:
    """The video's aspect ratio at H3's ~16 MP (vertical 9:16 unless the brief says else)."""
    m = re.search(r"(\d+)\s*:\s*(\d+)", pkg.asset_requirements.style_brief.aspect_ratio or "")
    return SIZES.get(f"{m.group(1)}:{m.group(2)}" if m else "", SIZES["9:16"])


@dataclass
class ShotPicks:
    """A shot's design picks as reference pictures, before they are chosen per sub-shot."""

    looks: list[FrameRef]  # per person: their look, or their face when they have no look
    faces: list[FrameRef]  # the faces of people who also have a look
    sets: list[FrameRef]
    props: list[FrameRef]
    missing: list[tuple[str | None, str]]  # (the character it is for, or None; what)

    def refs(self, people: set[str] | None = None,
             previous: FrameRef | None = None) -> list[FrameRef]:  # fmt: skip
        """The pictures for `people` (None: everyone), in the order the prompt names them,
        as dramer sends them: the previous keyframe, each person in their look, the set's
        wide view, the props, then faces while the nine-picture limit allows."""

        def mine(r: FrameRef) -> bool:
            return people is None or r.entity_id in people

        refs = [*([previous] if previous else []), *filter(mine, self.looks), *self.sets,
                *self.props][:MAX_REFERENCES]  # fmt: skip
        return refs + [f for f in self.faces if mine(f)][: MAX_REFERENCES - len(refs)]

    def missing_for(self, people: set[str] | None = None) -> list[str]:
        """What is missing for `people` (None: everyone); the set and props always count."""
        return [m for ch, m in self.missing if ch is None or people is None or ch in people]


def shot_picks(pkg: ProductionPackage, images: list[GeneratedImage], shot: Shot) -> ShotPicks:
    names = _names(pkg)
    reqs = {r.requirement_id: r for r in pkg.asset_requirements.items}
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


def single_plan(pkg: ProductionPackage, shot: Shot) -> ShotPlan:
    """The plan of a shot nobody has planned: the whole shot is one sub-shot."""
    return ShotPlan(
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        shot_id=shot.shot_id,
        sub_shots=[
            SubShot(index=1, start_ms=0, end_ms=shot.duration_ms, framing=shot.framing,
                    camera=shot.camera, action=shot.visual_action,
                    people=list(shot.character_ids))
        ],
    )  # fmt: skip


def plan_of(pkg: ProductionPackage, shot: Shot, stored: ShotPlan | None) -> ShotPlan:
    """The plan in force: the stored one when it has sub-shots, else the single one,
    which is busy while a first plan is being written (so nothing is drawn meanwhile)."""
    if stored is not None and stored.sub_shots:
        return stored
    plan = single_plan(pkg, shot)
    if stored is not None:
        plan.status = stored.status
    return plan


def current_sub(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, take: ShotTake
) -> SubShot | None:
    """The plan's sub-shot a take was drawn for, if the plan still has it unchanged. A
    take from before sub-shots was drawn for the whole shot: it matches the single plan."""
    sub = plan.sub(take.sub_shot) if take.sub_shot >= 1 else None
    drawn = take.spec or single_plan(pkg, shot).sub_shots[0]
    return sub if sub is not None and drawn == sub else None


def make_sub_shots(shot: Shot, rows: list[dict[str, Any]]) -> list[SubShot]:
    """Sub-shots from (start_ms, framing, camera, action, people) rows, checked: 1 to
    MAX_SUB_SHOTS, the first at 0, strictly increasing, each at least MIN_SUB_SHOT_MS,
    only the shot's people. Each ends where the next starts; the last at the shot's end."""
    if not 1 <= len(rows) <= MAX_SUB_SHOTS:
        raise ValueError(f"a shot has 1 to {MAX_SUB_SHOTS} sub-shots")
    starts = [int(r["start_ms"]) for r in rows]
    if starts[0] != 0:
        raise ValueError("the first sub-shot starts at 0 s")
    ends = [*starts[1:], shot.duration_ms]
    out = []
    for k, (row, start, end) in enumerate(zip(rows, starts, ends, strict=True), 1):
        if end - start < MIN_SUB_SHOT_MS:
            raise ValueError(
                f"sub-shot {k} is {max(0, end - start) / 1000:.1f} s; each runs at least "
                f"{MIN_SUB_SHOT_MS / 1000:.0f} s, in order, inside the shot's "
                f"{shot.duration_ms / 1000:.1f} s"
            )
        framing, action = str(row.get("framing", "")).strip(), str(row.get("action", "")).strip()
        if not framing or not action:
            raise ValueError(f"sub-shot {k} needs a framing and an action")
        people = list(dict.fromkeys(row.get("people") or []))
        if stray := [p for p in people if p not in shot.character_ids]:
            raise ValueError(f"sub-shot {k} lists {', '.join(stray)}, who isn't in the shot")
        out.append(SubShot(index=k, start_ms=start, end_ms=end, framing=framing,
                           camera=str(row.get("camera", "")).strip(), action=action,
                           people=people))  # fmt: skip
    return out


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
    changing or withdrawing one withdraws those drawn from another, down the chain."""
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
        for t in every:
            if (
                t.sub_shot == k
                and t.approved is not None
                and (not t.references or t.references[0].ref_id != want)
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


def sub_shot_references(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    shot: Shot,
    sub: SubShot,
    picks: ShotPicks | None = None,
) -> tuple[list[FrameRef], list[str]]:
    """(the pictures a sub-shot's keyframe is drawn from, what is missing): the picks of
    the people in this sub-shot, the set and the props, and from sub-shot 2 on, first the
    approved keyframe of the sub-shot before it. Chosen per sub-shot, then capped at nine,
    so a close-up keeps its person's face however crowded the shot is."""
    picks = picks or shot_picks(pkg, images, shot)
    people = set(sub.people)
    missing = picks.missing_for(people)
    previous = None
    if sub.index > 1:
        prev = approved_take(takes, shot.shot_id, sub.index - 1)
        if prev is None:
            missing.append(f"an approved keyframe for sub-shot {sub.index - 1}")
        else:
            previous = FrameRef(ref_id=frame_ref(prev), kind="previous_frame",
                                role=f"the approved keyframe of sub-shot {sub.index - 1}, "
                                "just before this cut", likeness=prev.likeness)  # fmt: skip
    return picks.refs(people, previous), missing


def _seeds(seeds: list[int] | None, n: int) -> list[int]:
    out = list(dict.fromkeys(seeds or []))[:n]
    return out + [random.randint(1, 2**31 - 1) for _ in range(n - len(out))]


def sub_shot_for(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plan: ShotPlan,
    sub_shot: int,
) -> tuple[SubShot, list[FrameRef]]:
    """(the sub-shot, the pictures its keyframe is drawn from), or NotReady saying why it
    can't be drawn now."""
    shot = shot_of(pkg, plan.shot_id)
    if plan.busy:
        raise NotReady(f"the sub-shots of {shot.shot_id} are still being planned")
    sub = plan.sub(sub_shot)
    if sub is None:
        raise NotReady(f"{shot.shot_id} has no sub-shot {sub_shot}")
    refs, missing = sub_shot_references(pkg, images, takes, shot, sub)
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
) -> ShotTake:
    """A new take on a sub-shot's keyframe: written by the model (steered by `direction`,
    and building on the newest take's prompt and check), or drawn from a prompt `version`
    as it is. Without `seeds`, a version is drawn with the newest take's seeds, so the two
    compare draw for draw."""
    shot = shot_of(pkg, plan.shot_id)
    shot_id = shot.shot_id
    if is_busy(takes, shot_id, sub_shot):
        raise NotReady(f"sub-shot {sub_shot} of {shot_id} is still being made")
    sub, refs = sub_shot_for(pkg, images, takes, plan, sub_shot)
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


# ---------------------------------------------------------------------------
# Prompt writer


WRITER_ROLE = """\
You write MiniMax-H3-Image edit prompts for the keyframes of a short drama. A shot is one \
video generation that may hold a few cuts (sub-shots); each sub-shot gets one keyframe, the \
picture the video model shows at the moment that sub-shot begins. The picture puts the \
sub-shot's people, exactly as they look in their reference pictures, into the set, framed \
and posed as the plan says. Character consistency matters most: every face, hairstyle and costume \
must match its picture. Our server renders the prompt exactly as written (no prompt \
expansion), so the prompt is the finished description. Follow the H3 rules and examples \
below.\
"""

WRITER_GUIDE = """\
## Your input

- `sub_shot`: the cut this keyframe opens: its number of `of`, its time in the clip, \
framing, camera, action and `people` (exactly these on screen, `people_count` of them, no \
one else). The keyframe is its first frame.
- `pictures`: one entry per <Picture N>, in order (they are attached), each with its \
`role`, its `define` sentence and its `relationship` sentence. From sub-shot 2 on, \
<Picture 1> is the approved keyframe of the sub-shot before: the same scene a moment \
before the cut.
- `shot`: the whole shot from the storyboard (framing, camera, action, continuity, notes, \
dialogue with its timing), for context.
- `scene`: where and when; `style`: medium, era, color and light; `aspect`: the canvas.
- `previous_prompt` and `fixes`: the last version's prompt and what was wrong with it (the \
quality check's findings on its best draw, or the rule check's). Keep what worked and fix \
every finding.
- `direction`: the user's steer, if any. It wins over everything but the content rules.

## How to write it

One paragraph in MiniMax's edit form, in this order:
1. **Definitions**: every picture's `define` sentence, as given, in picture order.
2. **The target overview**, one or two sentences starting "The target image is a still \
frame from a live-action short drama": the shot size and camera angle, then the layout: \
exactly how many people, who is screen-left, screen-right, foreground, background, and \
each body's orientation to the camera.
3. **Relationships**: every picture's `relationship` sentence, as given, in picture order. \
For a "partially preserved" one you may add, inside that sentence, which visible details of \
this sub-shot differ (a pose, a jacket now off, a cup now empty), and nothing else.
4. **The frozen moment**: each person's pose, which hand holds or touches what, gaze and \
expression at the moment this sub-shot begins; a speaking person's mouth slightly open. \
Then the set and props as this camera sees them.
5. **Light and lens**: the light's source, direction and quality as it falls on the people, \
and the lens feel (focal length, depth of field). End with "No other lettering is added."

Rules that matter for H3, which reads every word as something to draw:
- **Narrate the finished image.** Write "her trousers are charcoal", never an instruction \
("make", "keep", "retain", "frame", "ensure", "change", "remove", "avoid").
- **No negations.** H3 has no negative prompt: "no", "not", "without", "nobody", "never", \
"un-" words put the thing in the picture. Say what is there instead ("the bed is made, its \
pale duvet smooth"), with one exception: the closing "No other lettering is added."
- **Visible facts only.** No story, intent or safety words ("non-intimate", "a quiet \
conversation", "fully clothed"): describe what the camera sees.
- **Clothes come from the look pictures.** Each person wears exactly what their look \
picture shows, unless the sub-shot's action changes it; never invent or swap garments.
- **Faces come from the face pictures.** A face picture's sentence transfers that exact \
face; never weaken it to a style reference.
- Precise visual words ("a soft square jaw", "warm window light from screen-left"), not \
judgments ("beautiful", "cinematic", "high quality"). Never add people the sub-shot \
doesn't list. Stay under 3,500 characters.

## What may be drawn

{rules}

{mode}

## Output

Return {{"prompt": "...", "motion": "...", "notes": "..."}}:
- `prompt`: the prompt only, as plain text.
- `motion`: one sentence on how the sub-shot moves from this frame until its end.
- `notes`: one short sentence to the user on what will be hard to get right, or "".\
"""

# Keyframes draw the production's chosen designs, so the Experiment tab's restrictive mode
# (which forbids taking a face from a photo) would defeat them; the content rules still
# apply, and the runner refuses intimate scenes drawn from a likeness.
KEYFRAME_RULE = """\
Keyframes: the look, face, set and prop pictures are the production's chosen designs for \
this story. Carry each person's face and clothing over exactly; that is the point of a \
keyframe. Never write a real person's name. In a scene with intimacy, show only the \
staging `scene.intimacy.on_screen` describes, with each person dressed as their look \
picture shows: no nudity, undressing or sexual activity is drawn (describe what is there, \
not what is absent). Rules 1 to 3 above always apply.\
"""


class FramePromptOutput(BaseModel):
    prompt: str
    motion: str = ""
    notes: str = ""


def writer_instructions() -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    guide = WRITER_GUIDE.format(rules=CONTENT_RULES, mode=KEYFRAME_RULE)
    return f"{WRITER_ROLE}\n\n{load_h3_image_rules()}\n\n{load_h3_image_examples()}\n\n{guide}"


def shot_context(pkg: ProductionPackage, shot: Shot) -> dict[str, Any]:
    """What the writer and the judge are told about a shot."""
    names = _names(pkg)
    lines = {
        bt.dialogue.dialogue_id: bt.dialogue
        for ep in pkg.episodes
        for sc in ep.scenes
        for bt in sc.beats
        if bt.dialogue is not None
    }
    looks = {lk.look_id: lk for lk in (pkg.cast_sheet.looks if pkg.cast_sheet else [])}
    people = []
    for ch in shot.character_ids:
        ref = next((r for r in shot.character_look_refs if r.character_id == ch), None)
        look = looks.get(ref.look_id) if ref else None
        people.append({
            "name": names.get(ch, ch),
            "look": {"wardrobe": look.wardrobe, "hair_and_makeup": look.hair_and_makeup,
                     "condition": look.condition} if look else {},
        })  # fmt: skip
    sc = scene_of(pkg, shot)
    state = next((s for s in pkg.story_bible.location_states
                  if s.location_state_id == shot.location_state_id), None)  # fmt: skip
    scene: dict[str, Any] = {
        "place": names.get(shot.location_id, shot.location_id),
        "state": state.description if state else "",
    }
    if sc is not None:
        scene |= {"int_ext": sc.int_ext, "time_of_day": sc.time_of_day}
        if sc.intimacy is not None and sc.intimacy.level != "none":
            scene["intimacy"] = {"level": sc.intimacy.level, "on_screen": sc.intimacy.on_screen}
    sb = pkg.asset_requirements.style_brief
    return {
        "shot": {
            "shot_id": shot.shot_id,
            "framing": shot.framing,
            "camera": shot.camera,
            "action": shot.visual_action,
            "continuity": shot.continuity,
            "notes": shot.generation_notes,
            "duration_s": round(shot.duration_ms / 1000, 1),
            "people": people,
            "people_count": len(shot.character_ids),
            "props": [names.get(p.prop_id, p.prop_id) for p in shot.prop_state_refs],
            "dialogue": [
                {
                    "speaker": names.get(lines[c.dialogue_id].character_id, ""),
                    "line": lines[c.dialogue_id].text,
                    # When it is spoken, in seconds from the start of the clip.
                    "start_s": round(max(0, c.start_ms - shot.start_ms) / 1000, 2),
                    "end_s": round(min(shot.duration_ms, c.end_ms - shot.start_ms) / 1000, 2),
                }
                for c in shot.dialogue_cues
                if c.dialogue_id in lines
            ],
        },
        "scene": scene,
        "style": {
            "medium": sb.medium,
            "era_region": sb.era_region,
            "color_and_light": sb.color_and_light,
        },
    }


def sub_shot_context(pkg: ProductionPackage, sub: SubShot, of: int) -> dict[str, Any]:
    """The sub-shot a keyframe opens, for the writer and the judge."""
    names = _names(pkg)
    return {
        "index": sub.index,
        "of": of,
        "start_s": round(sub.start_ms / 1000, 3),
        "end_s": round(sub.end_ms / 1000, 3),
        "framing": sub.framing,
        "camera": sub.camera,
        "action": sub.action,
        "people": [{"name": names.get(c, c)} for c in sub.people],
        "people_count": len(sub.people),
    }


def take_sub(pkg: ProductionPackage, shot: Shot, take: ShotTake) -> SubShot:
    """The sub-shot a take was made for (takes from before sub-shots: the whole shot)."""
    return take.spec or single_plan(pkg, shot).sub_shots[0]


def picture_sentences(pkg: ProductionPackage, take: ShotTake) -> list[dict[str, Any]]:
    """Per <Picture N>: its role, and the definition and relationship sentences the prompt
    uses for it (MiniMax's edit form). Written here, not by the model, so every picture is
    used for what it is: a face picture always transfers that exact face."""
    names = _names(pkg)
    look_of = {r.entity_id: n for n, r in enumerate(take.references, 1) if r.kind == "look"}
    out = []
    for n, r in enumerate(take.references, 1):
        who = names.get(r.entity_id or "", "")
        subject = f"<Subject {n}>"
        match r.kind:
            case "previous_frame" | "start_frame":
                define = f"The film frame in <Picture {n}> is {subject}."
                rel = (f"{subject} is partially preserved: the set, the light, each person's "
                       "face, hair and clothing, and the props in their current state remain; "
                       "the camera position, framing and poses follow this description.")  # fmt: skip
            case "look":
                define = f"{who or 'The person'} in <Picture {n}> is {subject}."
                rel = (f"{subject} is partially preserved: the face, hair, build and every "
                       "garment and accessory remain; the pose, position and expression "
                       "follow this description.")  # fmt: skip
            case "face" if r.entity_id in look_of:
                scope = f"{who}'s face" if who else "The face"
                define = f"{scope} in <Picture {n}> is {subject}."
                rel = (f"{subject} supplies the exact facial features transferred to "
                       f"<Subject {look_of[r.entity_id]}>.")  # fmt: skip
            case "face":
                define = f"{who or 'The person'} in <Picture {n}> is {subject}."
                rel = (f"{subject} is partially preserved: the face, hair and build remain; "
                       "the clothing, pose, position and expression follow this "
                       "description.")  # fmt: skip
            case "set":
                define = f"The room in <Picture {n}> is {subject}."
                rel = (f"{subject} is partially preserved: the layout, materials, furniture "
                       "and palette remain; the camera position and the light follow this "
                       "description.")  # fmt: skip
            case _:
                define = f"{who or 'The object'} in <Picture {n}> is {subject}."
                rel = f"{subject} is fully preserved."
        out.append({"picture": f"<Picture {n}>", "role": r.role, "define": define,
                    "relationship": rel})  # fmt: skip
    return out


def template_prompt(pkg: ProductionPackage, take: ShotTake) -> str:
    """A usable prompt built by the program (offline, or when the writer fails)."""
    shot = shot_of(pkg, take.shot_id)
    sub = take_sub(pkg, shot, take)
    pictures = picture_sentences(pkg, take)
    count = len(sub.people)
    people = (f"exactly {count} {'person' if count == 1 else 'people'}" if count
              else "the empty set")  # fmt: skip
    camera = f", {sub.camera.rstrip('.')}" if sub.camera else ""
    target = (f"The target image is a still frame from a live-action short drama: "
              f"{sub.framing.rstrip('.')}{camera}, showing {people}, at the moment the "
              f"action begins: {sub.action.rstrip('.')}.")  # fmt: skip
    extra = [take.direction.rstrip(".") + "." if take.direction else ""]
    return " ".join(
        p for p in [*(x["define"] for x in pictures), target,
                    *(x["relationship"] for x in pictures), *extra, NO_OTHER_TEXT] if p
    )  # fmt: skip


# What the rule check flags (see prompt_findings).
_NEGATION = re.compile(
    r"\b(no|not|never|without|nobody|nothing|none|neither|nor|isn't|aren't|doesn't|don't)\b",
    re.IGNORECASE,
)
# Sentences that instruct the model instead of describing the image.
_COMMAND = re.compile(
    r"(?:^|[.!?]\s+)(make|keep|retain|ensure|frame|avoid|do not|don't)\b", re.IGNORECASE
)
# Words that state intent or safety instead of something visible.
_STORY = re.compile(r"\b(non-intimate|non-sexual|fully clothed|platonic)\b", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]")


def worth_repair(findings: list[str]) -> bool:
    """Whether findings justify a second writer call: anything but story words alone."""
    return any(not f.startswith("uses story") for f in findings)


def is_hard(finding: str) -> bool:
    """A finding that makes a prompt unusable, not just weaker."""
    return finding == "empty" or finding.startswith(("longer than", "not allowed", "cites"))


def prompt_findings(prompt: str, refs: list[FrameRef]) -> list[str]:
    """Where a keyframe prompt breaks the H3 image rules, as far as a program can tell:
    the edit form (every picture defined, the overview, one relationship each, the closing
    sentence), negations, instructions, story words, and a face picture used as anything
    but the face."""
    from .image_prompts import problems

    out = [
        f"cites {', '.join(over)}, which this sub-shot doesn't send (it sends {len(refs)})"
        if f.startswith("cites a picture") and (over := cited_beyond(prompt, len(refs)))
        else f
        for f in problems(prompt, len(refs))
    ]
    body = _QUOTED.sub("", prompt).replace(NO_OTHER_TEXT, "")
    if words := sorted({m.lower() for m in _NEGATION.findall(body)}):
        out.append(f"uses negation ({', '.join(words)}): H3 draws what is named; say what is there")
    if words := sorted({m.lower() for m in _COMMAND.findall(body)}):
        out.append(f"gives instructions ({', '.join(words)}): narrate the finished image instead")
    if words := sorted({m.lower() for m in _STORY.findall(body)}):
        out.append(f"uses story or intent words ({', '.join(words)}): describe what is seen")
    for n, r in enumerate(refs, 1):
        token = f"<Subject {n}>"
        if r.kind == "face" and re.search(rf"{re.escape(token)}[^.]*weak", prompt):
            out.append(f"weakens {token}, a face picture: it must transfer the exact face")
        if prompt.count(token) < 2:
            out.append(f"gives {token} no relationship sentence")
    return out


def fixture_frame_prompt(request: GenerationRequest) -> FramePromptOutput:
    """Offline stand-in for the writer: the template, passed in the context."""
    return FramePromptOutput(
        prompt=request.context.get("template_prompt", ""),
        motion="Offline sample: the action as the plan describes it.",
        notes="Offline sample: the built-in template.",
    )


# ---------------------------------------------------------------------------
# Sub-shot planner

PLAN_ROLE = """\
You are the director's assistant on a short drama made with an AI video model (MiniMax H3). \
Each storyboard shot is rendered as one video clip, and a dynamic clip may hold a few cuts \
inside it, written in the video prompt as `[Shot 1] ...` then `[Shot 2] At 00:02.000, the \
camera cuts to ...`. You decide where a shot's cuts go: its sub-shots. Each sub-shot then \
gets its own keyframe picture, which the video model shows the moment that sub-shot begins.\
"""

PLAN_GUIDE = """\
## Your input

`shot`: the storyboard shot (framing, camera, action, continuity, notes, its people and \
props, `duration_s`) and `dialogue`, each line with when it starts and ends in the clip \
(`start_s`, `end_s`) and who speaks. `scene`: where and when.

## How to cut

- Cut only for new information: a new subject, space, state, viewpoint or time. A small \
reframe or a push-in is a camera move inside one sub-shot, not a cut. Most held moments \
are one sub-shot; a shot whose framing or camera notes describe two setups (a wide, then a \
close-up on the reveal), a reaction, or a reveal of something new is two or three.
- At most {max_sub} sub-shots. The first starts at 0; each later one starts strictly after \
the one before, at least {min_s:.0f} s apart, and before the clip ends. A line of dialogue is \
best not cut in the middle: cut between lines, or keep the speaker on screen across the cut.
- Each sub-shot: `start_s` (seconds from the start of the clip, e.g. 2.5), `framing` (shot \
size, angle, who is where on screen), `camera` (still, pan, push-in, handheld...), \
`action` (what happens from that moment until the next cut, as visible movement), and \
`people`: the names of exactly the people on screen in it (from `shot.people`).
- Together the sub-shots tell the whole shot's action, in order, with nothing added.

Return {{"sub_shots": [{{"start_s": 0, "framing": "...", "camera": "...", "action": "...", \
"people": ["..."]}}, ...], "notes": "one short sentence to the user on why you cut it so"}}.\
"""


class PlannedSubShot(BaseModel):
    start_s: float = Field(ge=0)
    framing: str
    camera: str = ""
    action: str
    people: list[str] = Field(default_factory=list)


class PlanOutput(BaseModel):
    sub_shots: list[PlannedSubShot]
    notes: str = ""


def plan_instructions() -> str:
    return (
        PLAN_ROLE + "\n\n" + PLAN_GUIDE.format(max_sub=MAX_SUB_SHOTS, min_s=MIN_SUB_SHOT_MS / 1000)
    )


def plan_context(pkg: ProductionPackage, shot: Shot) -> dict[str, Any]:
    ctx = shot_context(pkg, shot)
    names = _names(pkg)
    ctx["shot"]["people"] = [names.get(c, c) for c in shot.character_ids]
    return ctx


def _who(pkg: ProductionPackage, shot: Shot, name: str) -> str:
    """The character a planner's name means, among the shot's people: the id, the exact
    name, else the one person whose name contains it or whose first name it starts with
    ("Lila", "Lila Chen", "Ms. Lila" for Lila). Raises when it means no one or several."""
    people = [c for c in pkg.story_bible.characters if c.character_id in shot.character_ids]
    said = name.strip().casefold()
    exact = [c for c in people if said in (c.character_id.casefold(), c.name.casefold())]
    if not exact:
        words = said.replace(".", " ").split()
        exact = [
            c for c in people
            if c.name.casefold() in said or said in c.name.casefold()
            or (words and c.name.split() and c.name.split()[0].casefold() in words)
        ]  # fmt: skip
    if len(exact) != 1:
        raise ValueError(f"the plan puts {name} on screen, who isn't one of the shot's people")
    return exact[0].character_id


def planned_sub_shots(pkg: ProductionPackage, shot: Shot, out: PlanOutput) -> list[SubShot]:
    """The planner's sub-shots, checked (names become character ids). A first cut a
    moment after 0 s is moved to 0 s; anything else that breaks the rules fails."""
    planned = sorted(out.sub_shots, key=lambda p: p.start_s)
    rows = []
    for k, p in enumerate(planned):
        start = 0 if k == 0 and p.start_s * 1000 < MIN_SUB_SHOT_MS else round(p.start_s * 1000)
        people = list(dict.fromkeys(_who(pkg, shot, n) for n in p.people))
        rows.append({"start_ms": start, "framing": p.framing, "camera": p.camera,
                     "action": p.action, "people": people})  # fmt: skip
    return make_sub_shots(shot, rows)


def fixture_plan(request: GenerationRequest) -> PlanOutput:
    """Offline stand-in for the planner: the whole shot is one sub-shot."""
    shot = request.context["shot"]
    return PlanOutput(
        sub_shots=[PlannedSubShot(start_s=0, framing=shot["framing"], camera=shot["camera"],
                                  action=shot["action"], people=shot["people"])],
        notes="Offline sample: one sub-shot.",
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Quality check (vision judge)

JUDGE_ROLE = """\
You are the continuity and quality checker of a short-drama production. You look at one \
generated keyframe (`<Frame>`) and the reference pictures it was made from (`<Picture N>`), \
and you say, item by item, whether it can go to video production. Be strict about \
character consistency and staging, as a script supervisor would: a frame the video model \
starts from carries every error into the clip.\
"""

JUDGE_GUIDE = """\
## Your input

- `<Frame>`: the keyframe to check (attached last).
- `pictures`: one entry per <Picture N> with its role, attached first. These are the truth \
for how each person, the set and each prop look.
- `sub_shot`: what the frame must show: the first moment of this cut, with its framing, \
camera, action and `people` (exactly these, `people_count` of them). From sub-shot 2 on, \
<Picture 1> is the approved keyframe of the sub-shot before: the set, light, clothes and \
props must match it, across the cut.
- `shot`: the whole shot (props, dialogue, continuity), for context.
- `prompt`: the prompt the frame was drawn from (for context; judge the frame, not the prompt).

## Check these items

- For each person in `sub_shot.people`: one item each for **identity** (face shape, features, \
skin, age: the same person as their picture) and **wardrobe** (clothes, hair, accessories \
as in their look picture). Name them: "Lila: face", "Lila: wardrobe".
- **count**: exactly `sub_shot.people_count` people with visible faces or bodies, no extras \
(small background silhouettes are fine if the shot implies them).
- **placement**: who is where (screen-left/right, foreground/background) and facing which \
way, as the framing and action say.
- **framing**: the shot size and camera angle.
- **action**: the pose and the moment match the sub-shot's action as it begins.
- **set**: the place matches the set picture (layout, materials, palette).
- **prop**: one item per listed prop: present and as in its picture.
- **defects**: hands, fingers, limbs, merged bodies, warped faces, broken perspective.
- **text**: no stray letters, captions or watermarks (lettering the scene needs is fine).
- **style**: a photographic live-action frame in the scene's light, not an illustration.

Each item: `passed`, `severity` (major: a viewer would notice or the clip would carry it; \
minor: small), and a concrete `finding` when it fails ("her blouse is blue; the look \
picture's is ivory silk"). Do not invent problems you can't see; an item you can't judge \
(a prop out of frame by design) passes.

Then a one-sentence `summary`, and `fix`: what the prompt should say differently so the \
next draw gets the failed items right (empty when everything passes).

Return {"items": [...], "summary": "...", "fix": "..."}.\
"""


class JudgeOutput(BaseModel):
    items: list[CheckItem]
    summary: str = ""
    fix: str = ""


def judge_instructions() -> str:
    return JUDGE_ROLE + "\n\n" + JUDGE_GUIDE


def fixture_check(request: GenerationRequest) -> JudgeOutput:
    """Offline stand-in for the judge: every item passes."""
    sub = request.context.get("sub_shot", {})
    items = [
        CheckItem(item=f"{p['name']}: {what}", kind=kind, passed=True)
        for p in sub.get("people", [])
        for what, kind in (("face", "identity"), ("wardrobe", "wardrobe"))
    ]
    items += [CheckItem(item=k, kind=k, passed=True)
              for k in ("count", "placement", "framing", "action", "set", "defects", "text")]  # fmt: skip
    return JudgeOutput(items=items, summary="Offline sample: not looked at.", fix="")


# ---------------------------------------------------------------------------
# Runner


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
    ) -> None:
        self.repo = repo
        self.media = media
        self.images = images
        self.text = text_client
        self._gpu = gpu or asyncio.Lock()
        self._planning = asyncio.Semaphore(4)  # sub-shot plans written at once
        self._tasks: set[asyncio.Task[None]] = set()

    def recover(self) -> int:
        error = "Interrupted by a server restart. Retry to run it again."
        return self.repo.fail_unfinished_shot_takes(error) + self.repo.fail_unfinished_shot_plans(
            error
        )

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
            request = GenerationRequest(
                stage=PLAN_STAGE,  # type: ignore[arg-type]
                instructions=plan_instructions(),
                context=plan_context(pkg, shot),
            )
            out = await self.text.generate(request, PlanOutput)
            plan.sub_shots = planned_sub_shots(pkg, shot, out)
            plan.notes, plan.source = out.notes.strip(), "model"
            plan.model = request.model or getattr(self.text, "model_id", None)
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
        """A take, then its automatic next rounds until a draw passes or none are left."""
        take = self.repo.get_shot_take(take_id)
        while take is not None and take.status == "pending":
            await self._run_take(take)
            nxt = next_round(take) if take.status == "done" else None
            if nxt is None:
                break
            self.repo.save_shot_take(nxt)
            take = nxt

    async def _run_take(self, take: ShotTake) -> None:
        pkg = self.repo.get_package(take.package_id, take.revision_id)
        try:
            if pkg is None:
                raise RuntimeError("the package revision is gone")
            shot = shot_of(pkg, take.shot_id)
            if not take.prompt:
                self._set(take, "writing")
                await self._write(pkg, shot, take)
            self._guard(pkg, shot, take)
            refs = await asyncio.to_thread(self._reference_bytes, take)
            self._set(take, "drawing")
            take.results = [d for d in take.results if d.status == "done"]
            for n in range(take.draws):
                if any(d.n == n for d in take.results):
                    continue  # kept from before a retry
                take.results.append(await self._draw(take, n, refs))
                take.results.sort(key=lambda d: d.n)
                self.repo.save_shot_take(take)
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
        context = {
            "sub_shot": sub_shot_context(pkg, take_sub(pkg, shot, take), self._of(take)),
            "pictures": picture_sentences(pkg, take),
            **shot_context(pkg, shot),
            "aspect": next((k for k, v in SIZES.items() if v == take.size), take.size),
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
                instructions=writer_instructions(),
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
            findings = prompt_findings(prompt, take.references)
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
            take.warnings = prompt_findings(template, take.references)
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
                take_id=take.take_id, findings=take.warnings,
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
        reason = upload_guard(take.prompt) if strict else text_guard(take.prompt)
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
        try:
            async with self._gpu:
                result = await self.images.generate(take.prompt, take.size, seed, refs)
        except Exception as exc:
            log.warning("shot draw %s/%d failed", take.take_id, n, exc_info=True)
            return FrameDraw(n=n, seed=seed, status="failed", error=str(exc)[:300])
        ext = "png" if result.content_type == "image/png" else "jpg"
        rel = f"keyframes/{take.package_id}/{take.take_id}_{n}.{ext}"
        await asyncio.to_thread(self._write_file, rel, result.data)
        thumb = await asyncio.to_thread(make_thumb, self.media, rel)
        view = await asyncio.to_thread(judge_view, self.media, rel)
        return FrameDraw(n=n, seed=seed, file=rel, thumb=thumb, view=view,
                         content_type=result.content_type, model=self.images.model_id)  # fmt: skip

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
            "sub_shot": sub_shot_context(pkg, take_sub(pkg, shot, take), self._of(take)),
            "pictures": [
                {"picture": f"<Picture {n}>", "role": r.role}
                for n, r in enumerate(take.references, 1)
            ],
            "prompt": take.prompt,
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


# ---------------------------------------------------------------------------
# Board view


def shot_board(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plans: list[ShotPlan],
    versions: list[PromptVersion] | None = None,
) -> list[dict[str, Any]]:
    """Every shot with its sub-shot plan, and per sub-shot what it draws from, what is
    missing, its prompt versions, its takes and its approved keyframe."""
    names = _names(pkg)
    stored = {p.shot_id: p for p in plans}
    out = []
    for shot in pkg.shot_list:
        own = stored.get(shot.shot_id)
        plan = plan_of(pkg, shot, own)
        mine = sorted((t for t in takes if t.shot_id == shot.shot_id), key=lambda t: t.created_at)
        picks = shot_picks(pkg, images, shot)  # once per shot, chosen per sub-shot below
        subs = []
        for sub in plan.sub_shots:
            refs, missing = sub_shot_references(pkg, images, mine, shot, sub, picks)
            ap = approved_take(mine, shot.shot_id, sub.index)
            subs.append({
                **sub.model_dump(),
                "timecode": sub.timecode,
                "people_names": [names.get(c, c) for c in sub.people],
                "references": [r.model_dump() for r in refs],
                "missing": missing,
                # Takes drawn for this sub-shot as it is now (not for an older plan's).
                "takes": [t.model_dump(mode="json") for t in mine
                          if t.sub_shot == sub.index and current_sub(pkg, shot, plan, t)],
                "approved": [ap.take_id, ap.approved] if ap is not None else None,
                # The number the next saved version gets (numbering runs across plans).
                "next_version": 1 + max((v.v for v in versions or []
                                         if (v.shot_id, v.sub_shot) == (shot.shot_id, sub.index)),
                                        default=0),
                # Prompt versions written for this sub-shot as it is now, oldest first.
                "versions": [v.model_dump(mode="json") for v in versions or []
                             if (v.shot_id, v.sub_shot, v.spec) == (shot.shot_id, sub.index, sub)],
            })  # fmt: skip
        out.append({
            "shot_id": shot.shot_id,
            "episode_id": shot.episode_id,
            "scene_id": shot.scene_id,
            "framing": shot.framing,
            "camera": shot.camera,
            "action": shot.visual_action,
            "duration_ms": shot.duration_ms,
            "people": [{"id": c, "name": names.get(c, c)} for c in shot.character_ids],
            "place": names.get(shot.location_id, shot.location_id),
            "intimacy": intimate_scene(pkg, shot),
            "plan": {
                "status": own.status if own else "done",
                "source": plan.source,
                "notes": own.notes if own else "",
                "error": own.error if own else None,
            },
            "sub_shots": subs,
            "busy": (own is not None and own.busy) or any(t.busy for t in mine),
        })  # fmt: skip
    return out
