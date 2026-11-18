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
import itertools
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from . import shot_reading
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
from .shot_reading import PersonState, SceneReading

log = logging.getLogger(__name__)

WRITE_STAGE = "shot_frames"
CHECK_STAGE = "shot_check"
PLAN_STAGE = "shot_plan"
MAX_SUB_SHOTS = 3
MIN_SUB_SHOT_MS = 1000
MIN_CLIP_S, MAX_CLIP_S = 4, 15  # H3 renders 4 to 15 s clips, in whole seconds
# A clip this long with two or more speakers is covered speaker by speaker, not held in one cut.
COVERAGE_MS = 6000
# The sub-shot spec's version in the export (05_assets/shot_keyframes/sub_shots.json): the
# contract video production writes its prompt from.
SPEC_VERSION = 1
Side = Literal["left", "center", "right"]
OffSide = Literal["left", "right"]
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
    # previous_shot: the approved keyframe of the shot before, in the same scene.
    kind: Literal["look", "face", "set", "prop", "previous_frame", "start_frame", "previous_shot"]
    entity_id: str | None = None  # the character, location or prop shown
    likeness: bool = False  # shows a real person's likeness (content rule 3)
    # For a person's picture chosen by reading the scene (shot_reading.py): how they look at
    # this moment, and what the picture already gets right.
    state: PersonState | None = None
    keep: list[str] = Field(default_factory=list)


class SubShot(BaseModel):
    """One cut inside a shot's video generation: `[Shot N]` of the video prompt."""

    index: int = Field(ge=1)  # 1-based, in time order
    start_ms: int = Field(ge=0)  # from the start of the shot (the clip)
    end_ms: int = Field(gt=0)
    framing: str
    camera: str = ""
    action: str
    people: list[str] = Field(default_factory=list)  # character ids on screen
    # Where each on-screen person is in the frame, and where each of the shot's other
    # people is off frame (for eyelines across the cut). Character id -> side.
    placement: dict[str, Side] = Field(default_factory=dict)
    off_screen: dict[str, OffSide] = Field(default_factory=dict)

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
    # The clip length asked for when planning (None: the storyboard's, see clip_ms).
    clip_ms: int | None = None
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
    # The pictures it was written for, in <Picture N> order: drawing it sends these, so its
    # picture numbers still mean what they meant when it was written.
    references: list[FrameRef] = Field(default_factory=list)
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


# Per package revision (revisions never change): names and lines of dialogue, which the
# board, the export and every writer and judge call look up shot by shot.
_REVISION_CACHE: dict[tuple[str, str, str], Any] = {}


def _cached(pkg: ProductionPackage, what: str, build: Any) -> Any:
    key = (pkg.package_id, pkg.revision_id, what)
    if key not in _REVISION_CACHE:
        if len(_REVISION_CACHE) > 256:
            _REVISION_CACHE.clear()
        _REVISION_CACHE[key] = build()
    return _REVISION_CACHE[key]


def _names(pkg: ProductionPackage) -> dict[str, str]:
    def build() -> dict[str, str]:
        names = {c.character_id: c.name for c in pkg.story_bible.characters}
        names |= {loc.location_id: loc.name for loc in pkg.story_bible.locations}
        names |= {p.prop_id: p.name for p in pkg.story_bible.props}
        return names

    return _cached(pkg, "names", build)


def _dialogue(pkg: ProductionPackage) -> dict[str, Any]:
    """Every line of dialogue of the package, by its id."""
    return _cached(pkg, "dialogue", lambda: {
        bt.dialogue.dialogue_id: bt.dialogue
        for ep in pkg.episodes for sc in ep.scenes for bt in sc.beats if bt.dialogue is not None
    })  # fmt: skip


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
        faces while the nine-picture limit allows."""
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
        refs = refs[:MAX_REFERENCES]
        return refs + faces[: MAX_REFERENCES - len(refs)]

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


def clip_ms(shot: Shot) -> int:
    """The shot's clip length as H3 renders it: whole seconds from 4 to 15, rounded up so
    every line the storyboard times inside the shot is inside the clip."""
    return max(MIN_CLIP_S, min(MAX_CLIP_S, -(-shot.duration_ms // 1000))) * 1000


def single_plan(pkg: ProductionPackage, shot: Shot) -> ShotPlan:
    """The plan of a shot nobody has planned: the whole clip is one sub-shot."""
    return ShotPlan(
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        shot_id=shot.shot_id,
        sub_shots=[
            SubShot(index=1, start_ms=0, end_ms=clip_ms(shot), framing=shot.framing,
                    camera=shot.camera, action=shot.visual_action,
                    people=list(shot.character_ids))
        ],
    )  # fmt: skip


def shot_lines(pkg: ProductionPackage, shot: Shot) -> list[dict[str, Any]]:
    """The shot's lines of dialogue, timed from the start of its clip (the one source of
    line timing for the planner, the writer, the judge, the checks and the export)."""
    names = _names(pkg)
    lines = _dialogue(pkg)
    out = []
    for c in shot.dialogue_cues:
        line = lines.get(c.dialogue_id)
        if line is None:
            continue
        out.append({
            "dialogue_id": c.dialogue_id,
            "speaker_id": line.character_id,
            "speaker": names.get(line.character_id, line.character_id),
            "text": line.text,
            "start_ms": max(0, c.start_ms - shot.start_ms),
            "end_ms": max(0, c.end_ms - shot.start_ms),
        })  # fmt: skip
    return sorted(out, key=lambda x: x["start_ms"])


def sub_shot_lines(lines: list[dict[str, Any]], sub: SubShot) -> list[dict[str, Any]]:
    """The lines spoken in a sub-shot (by their start), each saying whether its speaker is
    on screen, off screen (and on which side) or a voice from outside the shot."""
    out = []
    for x in lines:
        if not sub.start_ms <= x["start_ms"] < sub.end_ms:
            continue
        who = x["speaker_id"]
        where = ("on screen" if who in sub.people
                 else f"off screen {sub.off_screen[who]}" if who in sub.off_screen
                 else "voice")  # fmt: skip
        out.append({**x, "where": where})
    return out


class PlanProblems(ValueError):
    """A plan that isn't a clean spec; `problems` lists why, one sentence each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(" | ".join(problems))
        self.problems = problems


TOO_LONG = "the storyboard shot runs"  # the start of the problem a plan can't fix
ONE_CUT = "dialogue held in one cut"  # the start of the coverage problem
# A storyboard that says to hold the two-shot (in its framing, camera or notes).
_HOLD_TWO_SHOT = re.compile(r"\bhold(?:s|ing)?\b[^.;]{0,30}\btwo[- ]shot\b", re.IGNORECASE)


def advisory(problem: str) -> bool:
    """A problem flagged on the board and in the export but never refused on a save: a shot
    too long for H3 (no plan fixes it) and dialogue held in one cut (a person may hold the
    two-shot on purpose)."""
    return problem.startswith((TOO_LONG, ONE_CUT))


def holds_two_shot(shot: Shot) -> bool:
    """Whether the storyboard asks to keep the shot on one two-shot ("hold the two-shot")."""
    text = f"{shot.framing} {shot.camera} {shot.generation_notes} {shot.continuity}"
    return bool(_HOLD_TWO_SHOT.search(text))


def coverage_cut(lines: list[dict[str, Any]], clip: int) -> int | None:
    """Where a clip whose lines come from two or more people could cut to the next speaker:
    the first gap between two different speakers' lines with a moment at least
    MIN_SUB_SHOT_MS from both ends of the clip (the middle of the usable gap). None when the
    lines come from one person, or no such moment exists."""
    ordered = sorted(lines, key=lambda x: x["start_ms"])
    for a, b in itertools.pairwise(ordered):
        if a["speaker_id"] == b["speaker_id"]:
            continue
        lo = max(a["end_ms"], MIN_SUB_SHOT_MS)
        hi = min(b["start_ms"], clip - MIN_SUB_SHOT_MS)
        if lo <= hi:
            return (lo + hi) // 2
    return None


def check_plan(pkg: ProductionPackage, shot: Shot, subs: list[SubShot]) -> list[str]:
    """Where a sub-shot plan isn't a clean spec for video production (empty if it is):
    the clip's length, every line wholly inside the clip and inside one sub-shot (no cut in
    the middle of a line), each speaker on screen or placed off screen, every on-screen
    person (and only they) placed in the frame, each cut changing the framing, screen sides
    that hold across cuts (the 180-degree rule), and dialogue between two people covered
    speaker by speaker when the clip is long enough and a cut can fall between their lines
    (unless the storyboard holds the two-shot). See `advisory` for the problems that are
    flagged but never refused."""
    names = _names(pkg)
    out: list[str] = []
    clip = subs[-1].end_ms
    if shot.duration_ms > MAX_CLIP_S * 1000:
        out.append(f"{TOO_LONG} {shot.duration_ms / 1000:g} s, longer than H3's "
                   f"{MAX_CLIP_S} s clips: split it in the storyboard")  # fmt: skip
    if clip % 1000 or not MIN_CLIP_S * 1000 <= clip <= MAX_CLIP_S * 1000:
        out.append(f"the clip runs {clip / 1000:g} s, but H3 renders whole seconds from "
                   f"{MIN_CLIP_S} to {MAX_CLIP_S}")  # fmt: skip
    cuts = [s.start_ms for s in subs[1:]]
    in_shot = set(shot.character_ids)
    spoken: list[dict[str, Any]] = []  # the lines that start inside the clip
    for x in shot_lines(pkg, shot):
        said = f'{x["speaker"]}\'s line "{x["text"][:40]}"'
        if x["start_ms"] >= clip:
            out.append(f"{said} starts at {x['start_ms'] / 1000:g} s, after the clip ends")
            continue
        spoken.append(x)
        if x["end_ms"] > clip:
            out.append(f"{said} runs to {x['end_ms'] / 1000:g} s, past the clip's end at "
                       f"{clip / 1000:g} s")  # fmt: skip
        if mid := [c for c in cuts if x["start_ms"] < c < x["end_ms"]]:
            out.append(f"the cut at {mid[0] / 1000:g} s falls in the middle of {said} "
                       f"({x['start_ms'] / 1000:g}-{x['end_ms'] / 1000:g} s)")  # fmt: skip
        sub = next(s for s in subs if s.start_ms <= x["start_ms"] < s.end_ms)
        who = x["speaker_id"]
        if who in in_shot and who not in sub.people and who not in sub.off_screen:
            out.append(f"{x['speaker']} speaks in sub-shot {sub.index} but is neither on "
                       "screen nor placed off screen")  # fmt: skip
    on_screen_lines = [x for x in spoken if x["speaker_id"] in in_shot]
    if (
        len(subs) == 1
        and clip >= COVERAGE_MS
        and not holds_two_shot(shot)
        and (cut := coverage_cut(on_screen_lines, clip))
    ):
        out.append(f"{ONE_CUT}: two people speak in this {clip / 1000:g} s clip, so cover "
                       f"them speaker by speaker (2 or 3 sub-shots, e.g. a cut at "
                       f"{cut / 1000:g} s), or note \"hold the two-shot\" in the storyboard")  # fmt: skip
    for sub in subs:
        for who in sub.people:
            if who not in sub.placement:
                out.append(f"sub-shot {sub.index}: where is {names.get(who, who)} in the frame "
                           "(left, center or right)?")  # fmt: skip
        if extra := [w for w in sub.placement if w in in_shot and w not in sub.people]:
            out.append(f"sub-shot {sub.index} gives a frame position to "
                       f"{', '.join(names.get(w, w) for w in extra)}, who isn't on screen in it")  # fmt: skip
        if stray := [w for w in [*sub.placement, *sub.off_screen] if w not in in_shot]:
            out.append(f"sub-shot {sub.index} places {', '.join(stray)}, who isn't in the shot")
        if both := [w for w in sub.off_screen if w in sub.people]:
            out.append(f"sub-shot {sub.index} has {', '.join(names.get(w, w) for w in both)} "
                       "both on and off screen")  # fmt: skip
    for a, b in itertools.pairwise(subs):
        if " ".join(a.framing.lower().split()) == " ".join(b.framing.lower().split()):
            out.append(f"sub-shots {a.index} and {b.index} have the same framing; a cut must "
                       "change the view")  # fmt: skip
        for who in shot.character_ids:
            was = a.placement.get(who) if who in a.people else a.off_screen.get(who)
            now = b.placement.get(who) if who in b.people else b.off_screen.get(who)
            if {was, now} == {"left", "right"}:
                out.append(f"{names.get(who, who)} moves from frame {was} to frame {now} across "
                           f"the cut at {b.start_ms / 1000:g} s (the 180-degree rule)")  # fmt: skip
    return out


def plan_problems(pkg: ProductionPackage, shot: Shot, plan: ShotPlan) -> list[str]:
    """Where a shot's plan in force isn't a clean spec. An unplanned shot (the single
    plan) is only flagged for what planning would answer or nothing can: dialogue held in one
    cut, and a shot too long for H3."""
    problems = check_plan(pkg, shot, plan.sub_shots)
    return problems if plan.source != "single" else [p for p in problems if advisory(p)]


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
    if sub is None:
        return None
    if drawn == sub:
        return sub
    # The single plan's clip once ended at the storyboard's length, not whole seconds: a
    # keyframe drawn for it then is still the same view.
    same_view = drawn.model_copy(update={"end_ms": sub.end_ms}) == sub
    return sub if plan.source == "single" and same_view else None


def make_sub_shots(
    pkg: ProductionPackage,
    shot: Shot,
    rows: list[dict[str, Any]],
    clip: int | None = None,
    *,
    strict: bool = True,
) -> list[SubShot]:
    """Sub-shots from (start_ms, framing, camera, action, people, placement, off_screen)
    rows over a clip of `clip` ms (default: the shot's, see clip_ms). Each ends where the
    next starts; the last at the clip's end. Raises ValueError when they can't be a plan
    at all (1 to MAX_SUB_SHOTS, the first at 0, each at least MIN_SUB_SHOT_MS, a framing
    and an action each, only the shot's people). With `strict`, also raises PlanProblems
    for every rule of check_plan it breaks except the advisory ones (a shot too long for H3,
    dialogue held in one cut); without, all of them are left for the caller to flag."""
    if not 1 <= len(rows) <= MAX_SUB_SHOTS:
        raise ValueError(f"a shot has 1 to {MAX_SUB_SHOTS} sub-shots")
    clip = clip or clip_ms(shot)
    starts = [int(r["start_ms"]) for r in rows]
    if starts[0] != 0:
        raise ValueError("the first sub-shot starts at 0 s")
    ends = [*starts[1:], clip]
    out = []
    for k, (row, start, end) in enumerate(zip(rows, starts, ends, strict=True), 1):
        if end - start < MIN_SUB_SHOT_MS:
            raise ValueError(
                f"sub-shot {k} is {max(0, end - start) / 1000:.1f} s; each runs at least "
                f"{MIN_SUB_SHOT_MS / 1000:.0f} s, in order, inside the clip's {clip / 1000:g} s"
            )
        framing, action = str(row.get("framing", "")).strip(), str(row.get("action", "")).strip()
        if not framing or not action:
            raise ValueError(f"sub-shot {k} needs a framing and an action")
        people = list(dict.fromkeys(row.get("people") or []))
        if stray := [p for p in people if p not in shot.character_ids]:
            raise ValueError(f"sub-shot {k} lists {', '.join(stray)}, who isn't in the shot")
        out.append(SubShot(index=k, start_ms=start, end_ms=end, framing=framing,
                           camera=str(row.get("camera", "")).strip(), action=action,
                           people=people, placement=dict(row.get("placement") or {}),
                           off_screen=dict(row.get("off_screen") or {})))  # fmt: skip
    if strict and (problems := [p for p in check_plan(pkg, shot, out) if not advisory(p)]):
        raise PlanProblems(problems)
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
    reading: SceneReading | None = None,
) -> tuple[list[FrameRef], list[str]]:
    """(the pictures a sub-shot's keyframe is drawn from, what is missing): for each person
    in this sub-shot, the picture a fresh `reading` of the scene chose (with their state at
    this moment), else their storyboard look; the set and the props. First comes the
    approved keyframe of the sub-shot before it (sub-shot 2 on; it must be approved), or for
    sub-shot 1, of the previous shot of the scene when it has one (light, set dressing and
    everyone's state carry over the cut; not waited for). Chosen per sub-shot, then capped at
    nine, so a close-up keeps its person's face however crowded the shot is."""
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
        else:
            previous = FrameRef(ref_id=frame_ref(prev), kind="previous_frame",
                                role=f"the approved keyframe of sub-shot {sub.index - 1}, "
                                "just before this cut", likeness=prev.likeness)  # fmt: skip
    else:
        previous_shot = previous_shot_frame(pkg, takes, shot)
    return picks.refs(people, previous, chosen, previous_shot), missing


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
    refs, missing = sub_shot_references(pkg, images, takes, shot, sub, reading=reading)
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
) -> ShotTake:
    """A new take on a sub-shot's keyframe: written by the model (steered by `direction`,
    and building on the newest take's prompt and check), or drawn from a prompt `version`
    as it is. Without `seeds`, a version is drawn with the newest take's seeds, so the two
    compare draw for draw."""
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
framing, camera, action, `people` (exactly these on screen, `people_count` of them, no one \
else, each with their `frame` side and, when known, `now`: what they wear, their hair, \
what they hold and their condition at this moment, which wins over their picture), \
`off_screen` (the shot's other people and the side they are off, for eyelines) and the \
`lines` spoken in it. The keyframe is its first frame: \
put each person on their side, looking toward where the others are.
- `pictures`: one entry per <Picture N>, in order (they are attached), each with its \
`role`, its `define` sentence, its `relationship` sentence and `likeness` (true only for a \
picture of a real person). From sub-shot 2 on, \
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
the fictional characters of this story, however photographic they look. Carry each \
person's face and clothing over exactly; that is the point of a keyframe. Never write a \
real person's name.

Rule 3 applies only to a person whose picture has `likeness: true` (it shows a real \
person). Every other person is a fictional character: don't move, cover up or restage them \
on a guess that a picture shows a real person, and stage their scene as the plan and the \
direction say.

Clothes follow the look pictures unless the action or the user's `direction` changes them \
(a jacket off, a man bare-chested in bed); follow such a direction within rules 1 and 2. In \
a scene with intimacy, show the staging `scene.intimacy.on_screen` describes: no nudity \
(no genitals, no woman's bare breasts) and no sexual activity is drawn; describe what is \
there, not what is absent. Rules 1 to 3 above always apply.\
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
            # When each line is spoken, in seconds from the start of the clip.
            "dialogue": [
                {
                    "speaker": x["speaker"],
                    "line": x["text"],
                    "start_s": x["start_ms"] / 1000,
                    "end_s": x["end_ms"] / 1000,
                }
                for x in shot_lines(pkg, shot)
            ],
        },
        "scene": scene,
        "style": {
            "medium": sb.medium,
            "era_region": sb.era_region,
            "color_and_light": sb.color_and_light,
        },
    }


def sub_shot_context(
    pkg: ProductionPackage,
    shot: Shot,
    sub: SubShot,
    of: int,
    states: dict[str, PersonState] | None = None,
) -> dict[str, Any]:
    """The sub-shot a keyframe opens, for the writer and the judge: its view, who is where
    in the frame and off it, and the lines spoken in it."""
    names = _names(pkg)
    return {
        "index": sub.index,
        "of": of,
        "start_s": round(sub.start_ms / 1000, 3),
        "end_s": round(sub.end_ms / 1000, 3),
        "framing": sub.framing,
        "camera": sub.camera,
        "action": sub.action,
        "people": [
            {
                "name": names.get(c, c),
                "frame": sub.placement.get(c, ""),
                **({"now": states[c].model_dump()} if states and c in states else {}),
            }
            for c in sub.people
        ],
        "people_count": len(sub.people),
        "off_screen": [
            {"name": names.get(c, c), "side": side} for c, side in sub.off_screen.items()
        ],
        "lines": [
            {"speaker": x["speaker"], "line": x["text"], "where": x["where"]}
            for x in sub_shot_lines(shot_lines(pkg, shot), sub)
        ],
    }


def take_states(take: ShotTake) -> dict[str, PersonState]:
    """Each person's state at this moment, as the take's pictures were chosen for it."""
    return {r.entity_id: r.state for r in take.references if r.entity_id and r.state}


def take_sub(pkg: ProductionPackage, shot: Shot, take: ShotTake) -> SubShot:
    """The sub-shot a take was made for (takes from before sub-shots: the whole shot)."""
    return take.spec or single_plan(pkg, shot).sub_shots[0]


def picture_sentences(pkg: ProductionPackage, take: ShotTake) -> list[dict[str, Any]]:
    """Per <Picture N>: its role, and the definition and relationship sentences the prompt
    uses for it (MiniMax's edit form). Written here, not by the model, so every picture is
    used for what it is: a face picture always transfers that exact face. A person's picture
    chosen by reading the scene keeps the face, build and what it already gets right, and
    states how they look at this moment (shot_reading.py), so what the picture shows that
    the moment doesn't (a bag in hand, pinned hair) is not carried over. Without a reading,
    the garments carry over but things held only appear where the action puts them."""
    names = _names(pkg)

    def to_state(subject: str, who: str, r: FrameRef) -> str:
        kept = ", ".join(["the face", "build", *r.keep])
        assert r.state is not None
        return (f"{subject} is partially preserved: {kept} remain; "
                f"{r.state.sentence(who or 'the person')}; the pose, position and expression "
                "follow this description.")  # fmt: skip

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
            case "previous_shot":
                define = f"The film frame in <Picture {n}> is {subject}."
                rel = (f"{subject} serves as the reference for the set, the light and the props "
                       "as they are at this moment; the people, their clothing and the camera "
                       "follow this description.")  # fmt: skip
            case "look" if r.state is not None:
                define = f"{who or 'The person'} in <Picture {n}> is {subject}."
                rel = to_state(subject, who, r)
            case "look":
                define = f"{who or 'The person'} in <Picture {n}> is {subject}."
                rel = (f"{subject} is partially preserved: the face, hair, build and every "
                       "garment remain; anything held in the picture appears only where the "
                       "action puts it; the pose, position and expression follow this "
                       "description.")  # fmt: skip
            case "face" if r.entity_id in look_of:
                scope = f"{who}'s face" if who else "The face"
                define = f"{scope} in <Picture {n}> is {subject}."
                rel = (f"{subject} supplies the exact facial features transferred to "
                       f"<Subject {look_of[r.entity_id]}>.")  # fmt: skip
            case "face" if r.state is not None:
                define = f"{who or 'The person'} in <Picture {n}> is {subject}."
                rel = to_state(subject, who, r)
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
                    "relationship": rel, "likeness": r.likeness})  # fmt: skip
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
    names = _names(pkg)
    where = "; ".join(f"{names.get(c, c)} frame {side}" for c, side in sub.placement.items())
    target = (f"The target image is a still frame from a live-action short drama: "
              f"{sub.framing.rstrip('.')}{camera}, showing {people}"
              f"{f' ({where})' if where else ''}, at the moment the action begins: "
              f"{sub.action.rstrip('.')}.")  # fmt: skip
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


def prompt_findings(prompt: str, refs: list[FrameRef], fixed: tuple[str, ...] = ()) -> list[str]:
    """Where a keyframe prompt breaks the H3 image rules, as far as a program can tell:
    the edit form (every picture defined, the overview, one relationship each, the closing
    sentence), negations, instructions, story words, and a face picture used as anything
    but the face. The wording checks skip the `fixed` sentences the program wrote (a
    person's state quotes the costume notes, which may say "not")."""
    from .image_prompts import problems

    out = [
        f"cites {', '.join(over)}, which this sub-shot doesn't send (it sends {len(refs)})"
        if f.startswith("cites a picture") and (over := cited_beyond(prompt, len(refs)))
        else f
        for f in problems(prompt, len(refs))
    ]
    body = prompt
    for sentence in fixed:
        body = body.replace(sentence, ".")  # a sentence boundary stays where it was
    body = _QUOTED.sub("", body).replace(NO_OTHER_TEXT, "")
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

`shot`: the storyboard shot (framing, camera, action, continuity, notes, its `people` and \
props) and `clip_s`, the clip's length in seconds. `lines`: each line of dialogue with its \
speaker and when it starts and ends in the clip (`start_s`, `end_s`); a speaker who isn't \
one of `shot.people` is a voice from outside the shot. `scene`: where and when. `problems`: \
what was wrong with your last plan, if any; fix every one.

Your plan is the spec video production renders from: its prompt follows your cuts, \
timestamps, framing and screen positions exactly, and each cut gets a keyframe drawn from \
your description. Make it clean and complete.

## How to cut

- **Cut for new information**: a new subject, space, state, viewpoint or time. A small \
reframe or a push-in is a camera move inside one sub-shot, not a cut.
- **Dialogue between people is covered speaker by speaker**: cut to the speaker (a single \
or an over-the-shoulder) shortly before their line starts, and stay on them until it ends; \
a reaction can follow in the gap between lines. Keep a two-shot for a line only when the \
storyboard's framing or notes ask to hold both people.
- **A clip of {coverage_s:g} s or more in which two or more people on screen speak is 2 \
or 3 sub-shots** (e.g. the two-shot for the first line, then a single of the person who answers), \
unless the storyboard's framing or notes say to hold the two-shot.
- **Never cut in the middle of a line.** Every cut falls between lines (or before the first \
or after the last).
- **Each cut changes the view**: a different shot size, angle or subject. Two sub-shots \
in a row never share a framing.
- At most {max_sub} sub-shots. The first starts at 0; each later one at least {min_s:.0f} s \
after the one before and before the clip ends. A held moment with one view is one \
sub-shot.

## Each sub-shot

- `start_s`: seconds from the start of the clip, e.g. 1.2.
- `framing`: shot size, angle and who is where, e.g. "Close-up of Lila, frame left, \
looking off frame right".
- `camera`: still, pan, push-in, handheld...
- `action`: what happens from this cut to the next, as visible movement, with the lines \
spoken in it.
- `people`: the names of exactly the people on screen in it.
- `placement`: for each person on screen, where they are in the frame: "left", "center" or \
"right".
- `off_screen`: for each of the shot's other people, the side of the frame they are off: \
"left" or "right" (where the on-screen people look to see them).

Screen sides hold across cuts (the 180-degree rule): a person who is on the right in the \
two-shot stays on the right in their single and is off frame right in the other person's \
single. Together the sub-shots tell the whole shot's action, in order, with nothing added.

Return {{"sub_shots": [{{"start_s": 0, "framing": "...", "camera": "...", "action": "...", \
"people": ["..."], "placement": {{"<name>": "left"}}, "off_screen": {{"<name>": "right"}}}}, \
...], "notes": "one short sentence to the user on why you cut it so"}}.\
"""


class PlannedSubShot(BaseModel):
    start_s: float = Field(ge=0)
    framing: str
    camera: str = ""
    action: str
    people: list[str] = Field(default_factory=list)
    placement: dict[str, str] = Field(default_factory=dict)  # name -> left | center | right
    off_screen: dict[str, str] = Field(default_factory=dict)  # name -> left | right


class PlanOutput(BaseModel):
    sub_shots: list[PlannedSubShot]
    notes: str = ""


def plan_instructions() -> str:
    return (
        PLAN_ROLE
        + "\n\n"
        + PLAN_GUIDE.format(
            max_sub=MAX_SUB_SHOTS, min_s=MIN_SUB_SHOT_MS / 1000, coverage_s=COVERAGE_MS / 1000
        )
    )


def plan_context(pkg: ProductionPackage, shot: Shot, clip: int | None = None) -> dict[str, Any]:
    ctx = shot_context(pkg, shot)
    names = _names(pkg)
    ctx["shot"]["people"] = [names.get(c, c) for c in shot.character_ids]
    ctx["shot"].pop("dialogue", None)  # as `lines`, timed in the clip
    ctx["clip_s"] = (clip or clip_ms(shot)) // 1000
    ctx["lines"] = [
        {"speaker": x["speaker"], "line": x["text"], "start_s": x["start_ms"] / 1000,
         "end_s": x["end_ms"] / 1000}
        for x in shot_lines(pkg, shot)
    ]  # fmt: skip
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


def _sides(
    pkg: ProductionPackage,
    shot: Shot,
    given: dict[str, str],
    allowed: tuple[str, ...],
    what: str,
) -> dict[str, str]:
    """A planner's {name: side} as {character id: side}, sides checked."""
    out = {}
    for name, side in given.items():
        side = side.strip().lower().replace("centre", "center")
        if side not in allowed:
            raise ValueError(f"{what} for {name} is {side!r}, not one of {allowed}")
        out[_who(pkg, shot, name)] = side
    return out


def planned_sub_shots(
    pkg: ProductionPackage,
    shot: Shot,
    out: PlanOutput,
    clip: int | None = None,
    *,
    strict: bool = True,
) -> list[SubShot]:
    """The planner's sub-shots (names become character ids), checked as make_sub_shots
    checks them. A first cut a moment after 0 s is moved to 0 s."""
    planned = sorted(out.sub_shots, key=lambda p: p.start_s)
    rows = []
    for k, p in enumerate(planned):
        start = 0 if k == 0 and p.start_s * 1000 < MIN_SUB_SHOT_MS else round(p.start_s * 1000)
        rows.append({
            "start_ms": start, "framing": p.framing, "camera": p.camera, "action": p.action,
            "people": list(dict.fromkeys(_who(pkg, shot, n) for n in p.people)),
            "placement": _sides(pkg, shot, p.placement, ("left", "center", "right"),
                                "the frame position"),
            "off_screen": _sides(pkg, shot, p.off_screen, ("left", "right"), "the off-screen side"),
        })  # fmt: skip
    return make_sub_shots(pkg, shot, rows, clip, strict=strict)


def spread(people: list[str]) -> dict[str, str]:
    """Default frame positions for people in one view: left to right."""
    sides = {1: ["center"], 2: ["left", "right"], 3: ["left", "center", "right"]}
    order = sides.get(len(people)) or [
        ("left", "center", "right")[i % 3] for i in range(len(people))
    ]
    return dict(zip(people, order, strict=True))


def fixture_plan(request: GenerationRequest) -> PlanOutput:
    """Offline stand-in for the planner: the whole shot is one sub-shot."""
    shot = request.context["shot"]
    return PlanOutput(
        sub_shots=[PlannedSubShot(start_s=0, framing=shot["framing"], camera=shot["camera"],
                                  action=shot["action"], people=shot["people"],
                                  placement=spread(shot["people"]))],
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
- `direction`: what the user asked of this take, if anything ("his shirt off", "tighter on \
her"). It overrides the look pictures where they disagree.

## Check these items

- For each person in `sub_shot.people`: one item each for **identity** (face shape, features, \
skin, age: the same person as their picture) and **wardrobe** (clothes, hair, what they \
hold). When the person has `now`, judge the wardrobe against it, not the picture: the \
picture may show a bag or pinned hair the moment doesn't have. Name them: "Lila: face", \
"Lila: wardrobe".
- **count**: exactly `sub_shot.people_count` people with visible faces or bodies, no extras \
(small background silhouettes are fine if the shot implies them).
- **placement**: who is where (each person's `frame` side in `sub_shot.people`, \
foreground/background) and looking toward the off-screen people's side, as the framing \
and action say.
- **framing**: the shot size and camera angle.
- **action**: the pose and the moment match the sub-shot's action as it begins.
- **set**: the place matches the set picture (layout, materials, palette).
- **prop**: one item per listed prop: present and as in its picture.
- **defects**: hands, fingers, limbs, merged bodies, warped faces, broken perspective.
- **text**: no stray letters, captions or watermarks (lettering the scene needs is fine).
- **style**: a photographic live-action frame in the scene's light, not an illustration.
- **direction**: when `direction` is given, one item (kind "action", named "Direction: \
<what was asked>") that fails, major, when the frame doesn't do what was asked. Judge the \
wardrobe against the look pictures as changed by the direction.

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
        self._reading_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

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
        refs, missing = sub_shot_references(
            pkg,
            self.repo.list_images(pkg.package_id, pkg.revision_id),
            self.repo.list_shot_takes(pkg.package_id, pkg.revision_id),
            shot,
            take_sub(pkg, shot, take),
            reading=reading,
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
                await self._choose_pictures(pkg, shot, take)
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
        pictures = picture_sentences(pkg, take)
        # The program's own sentences: the wording checks leave them to their authors.
        fixed = tuple(x[k] for x in pictures for k in ("define", "relationship"))
        context = {
            "sub_shot": sub_shot_context(
                pkg, shot, take_sub(pkg, shot, take), self._of(take), take_states(take)
            ),
            "pictures": pictures,
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
            findings = prompt_findings(prompt, take.references, fixed)
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
            take.warnings = prompt_findings(template, take.references, fixed)
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


# ---------------------------------------------------------------------------
# Board view


def shot_board(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plans: list[ShotPlan],
    versions: list[PromptVersion] | None = None,
    readings: list[SceneReading] | None = None,
) -> list[dict[str, Any]]:
    """Every shot with its sub-shot plan and its scene's reading, and per sub-shot what it
    draws from (each person's chosen picture, state and why), what is missing, its prompt
    versions, its takes and its approved keyframe."""
    names = _names(pkg)
    stored = {p.shot_id: p for p in plans}
    by_scene = {r.scene_id: r for r in readings or []}
    fresh_of: dict[str, SceneReading | None] = {}
    for scene_id in {s.scene_id for s in pkg.shot_list}:
        print_ = shot_reading.fingerprint(pkg, scene_id, stored, images)
        fresh_of[scene_id] = shot_reading.fresh(by_scene.get(scene_id), print_)
    out = []
    for shot in pkg.shot_list:
        reading = fresh_of.get(shot.scene_id)
        pool = {c.image_id: c for c in reading.candidates} if reading else {}
        own = stored.get(shot.shot_id)
        plan = plan_of(pkg, shot, own)
        mine = sorted((t for t in takes if t.shot_id == shot.shot_id), key=lambda t: t.created_at)
        picks = shot_picks(pkg, images, shot)  # once per shot, chosen per sub-shot below
        lines = shot_lines(pkg, shot)
        subs = []
        for sub in plan.sub_shots:
            # Every take: the previous shot's keyframe is among them.
            refs, missing = sub_shot_references(pkg, images, takes, shot, sub, picks, reading)
            choices = []
            for who in sub.people:
                c = reading.choice(shot.shot_id, sub.index, who) if reading else None
                if c is None:
                    continue
                choices.append({
                    "character_id": who,
                    "name": names.get(who, who),
                    "image_id": c.image_id,
                    "state": c.state.model_dump(),
                    "keep": c.keep,
                    "reason": c.reason,
                    "source": c.source,
                    "shows": pool[c.image_id].shows if c.image_id in pool else "",
                    "candidates": [{"image_id": x.image_id, "label": x.label, "shows": x.shows}
                                   for x in pool.values() if x.character_id == who],
                })  # fmt: skip
            ap = approved_take(mine, shot.shot_id, sub.index)
            subs.append({
                **sub.model_dump(),
                "timecode": sub.timecode,
                "people_names": [names.get(c, c) for c in sub.people],
                "lines": sub_shot_lines(lines, sub),
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
                "choices": choices,
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
            "clip_ms": plan.sub_shots[-1].end_ms,
            # The scene's reading of who looks how (shot_reading.py): fresh, or not yet.
            "reading": {
                "scene_id": shot.scene_id,
                "status": (r := by_scene.get(shot.scene_id)) and r.status,
                "fresh": reading is not None,
                "error": r.error if r else None,
                "model": r.model if r else None,
            },
            "plan": {
                "status": own.status if own else "done",
                "source": plan.source,
                # Where the plan isn't a clean spec for video production yet.
                "problems": plan_problems(pkg, shot, plan),
                "notes": own.notes if own else "",
                "error": own.error if own else None,
            },
            "sub_shots": subs,
            "busy": (own is not None and own.busy) or any(t.busy for t in mine),
        })  # fmt: skip
    return out
