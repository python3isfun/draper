"""Shot keyframes: one checked picture per shot, made from the design picks, for video.

The video model keeps characters consistent and moves them as intended only as well as its
inputs show it. So before a shot goes to video production (dramer), each shot gets a
**start frame**: an H3 edit that puts the shot's people, in their picked looks, into the
picked set with its props, framed and posed as the storyboard says. A shot whose action
moves (someone enters, turns, hands something over) can also get an **end frame**, an
edit of the approved start frame, so the clip is pinned at both ends.

Each frame goes through a quality loop:

1. **Write**: the text model writes the edit prompt from the shot (framing, camera,
   action, who is where) and the reference pictures (`<Picture N>`).
2. **Draw**: a few draws with chosen or random seeds.
3. **Check**: a vision judge compares each draw with the reference pictures and the shot,
   item by item (each person's face, hair and wardrobe; the set and props; how many people
   and where; framing; the action; defects; stray text). The score and the verdict are
   computed here from the items, not taken from the judge.
4. **Revise**: if no draw passes, the failed items and the judge's fix go back to the
   writer for the next round, with the same seeds so rounds compare like for like; up to
   `MAX_ROUNDS` rounds, then a person decides.

A person approves one draw per frame; only approved frames are exported. Keyframes are
production assets, so the restrictive content rules always apply (as on the design board),
and a shot in a scene staged as implied or simulated intimacy is never drawn from a pick
that shows a real person's likeness (content rule 3).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .content_guard import RULES as CONTENT_RULES
from .content_guard import GuardRefused, mode_rule, text_guard, upload_guard
from .experiment import MAX_PROMPT_CHARS, SIZES, cited_beyond
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
MAX_ROUNDS = 3  # automatic rounds before a person decides
MAX_DRAWS = 4
DEFAULT_DRAWS = 2
PASS_SCORE = 80
MAJOR_PENALTY = 25
MINOR_PENALTY = 8
JUDGE_PX = 1024  # long edge of the draw the judge sees
NO_OTHER_TEXT = "No other lettering is added."

Frame = Literal["start", "end"]
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

    ref_id: str  # a design image (img_...) or an approved start frame (frame:<take>:<n>)
    role: str  # "Lila in her morning look, full body", "the approved start frame"
    kind: Literal["look", "face", "set", "prop", "start_frame"]
    entity_id: str | None = None  # the character, location or prop shown
    likeness: bool = False  # shows a real person's likeness (content rule 3)


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
    """One round of one frame of one shot: a prompt and its draws, each checked."""

    take_id: str
    package_id: str
    revision_id: str
    shot_id: str
    frame: Frame = "start"
    round: int = 1
    run_id: str  # the first take of an automatic run; its later rounds share it
    status: TakeStatus = "pending"
    prompt: str = ""
    prompt_source: Literal["pending", "model", "template", "user"] = "pending"
    prompt_model: str | None = None
    notes: str = ""  # the writer's note to the user
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
    # The writer's reading of the shot (start frames): how it moves, and whether an end
    # frame is worth making.
    motion: str = ""
    end_frame: bool | None = None
    approved: int | None = None  # the approved draw (one approved draw per shot and frame)
    likeness: bool = False
    refused: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

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
    """The shot can't be drawn yet (a design pick is missing, or no start frame)."""


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


def shot_references(
    pkg: ProductionPackage, images: list[GeneratedImage], shot: Shot
) -> tuple[list[FrameRef], list[str]]:
    """(the pictures a start frame is drawn from, what is missing). In the order the
    prompt names them, as dramer sends them: each person in their look, the set's wide
    view, the props, then the faces while the nine-picture limit allows. A person with no
    look image is sent by their face."""
    names = _names(pkg)
    reqs = {r.requirement_id: r for r in pkg.asset_requirements.items}
    wanted = [reqs[r] for r in shot.asset_requirement_ids if r in reqs]
    missing: list[str] = []

    def pick(req: Any) -> GeneratedImage | None:
        view = SHOT_VIEWS.get(req.asset_type)
        d = next((d for d in req.deliverables if d.view == view), None)
        if d is None:
            return None
        img = selected_image(images, (req.requirement_id, d.deliverable_id))
        if img is None:
            missing.append(f"{_req_title(req, names)} ({view})")
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
        look = pick(look_req) if look_req else None
        face = pick(face_req) if face_req else None
        if look is not None:
            looks.append(FrameRef(ref_id=look.image_id, role=f"{name} in this shot's look, "
                                  "full body", kind="look", entity_id=ch,
                                  likeness=look.likeness))  # fmt: skip
        if face is not None:
            ref = FrameRef(ref_id=face.image_id, role=f"{name}'s face", kind="face",
                           entity_id=ch, likeness=face.likeness)  # fmt: skip
            (faces if look is not None else looks).append(ref)
        if look is None and face is None and not (look_req or face_req):
            missing.append(f"{name} (no design image is planned)")
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
    refs = (looks + sets + props)[:MAX_REFERENCES]
    refs += faces[: MAX_REFERENCES - len(refs)]
    return refs, missing


def _req_title(req: Any, names: dict[str, str]) -> str:
    entity = req.subject_refs[0].entity_id if req.subject_refs else req.requirement_id
    return names.get(entity, entity)


def is_busy(takes: list[ShotTake], shot_id: str, frame: Frame) -> bool:
    return any(t.busy for t in takes if t.shot_id == shot_id and t.frame == frame)


def approved_take(takes: list[ShotTake], shot_id: str, frame: Frame) -> ShotTake | None:
    return next(
        (t for t in takes if t.shot_id == shot_id and t.frame == frame and t.approved is not None),
        None,
    )


def approval_changes(take: ShotTake, n: int | None, others: list[ShotTake]) -> list[ShotTake]:
    """The takes whose `approved` changes when draw `n` of `take` is approved (None:
    withdrawn); `others` are the shot's other takes. One approved draw per shot and frame,
    and an approved end frame must be drawn from the approved start frame: changing or
    withdrawing the start frame withdraws an end frame drawn from another one."""
    changed = []
    if take.approved != n:
        take.approved = n
        changed.append(take)
    if n is not None:
        for t in others:
            if t.frame == take.frame and t.approved is not None:
                t.approved = None
                changed.append(t)
    if take.frame == "start":
        starts = [t for t in [take, *others] if t.frame == "start" and t.approved is not None]
        current = {f"frame:{t.take_id}:{t.approved}" for t in starts}
        for t in others:
            if (
                t.frame == "end"
                and t.approved is not None
                and not (t.references and t.references[0].ref_id in current)
            ):
                t.approved = None
                changed.append(t)
    return changed


def _seeds(seeds: list[int] | None, n: int) -> list[int]:
    out = list(dict.fromkeys(seeds or []))[:n]
    return out + [random.randint(1, 2**31 - 1) for _ in range(n - len(out))]


def plan_take(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    shot_id: str,
    *,
    frame: Frame = "start",
    draws: int = DEFAULT_DRAWS,
    seeds: list[int] | None = None,
    direction: str | None = None,
    prompt: str | None = None,
    auto: bool = True,
    max_rounds: int = MAX_ROUNDS,
) -> ShotTake:
    """A new take on a shot's frame: written by the model (steered by `direction`, and
    building on the newest take's prompt and check), or the user's own `prompt`."""
    shot = shot_of(pkg, shot_id)
    if is_busy(takes, shot_id, frame):
        raise NotReady(f"the {frame} frame of {shot_id} is still being made")
    refs, missing = shot_references(pkg, images, shot)
    if missing:
        raise NotReady("pick design images first: " + "; ".join(missing))
    if frame == "end":
        start = approved_take(takes, shot_id, "start")
        if start is None:
            raise NotReady("approve a start frame first: the end frame is drawn from it")
        first = FrameRef(ref_id=f"frame:{start.take_id}:{start.approved}",
                         role="the approved start frame of this shot", kind="start_frame",
                         likeness=start.likeness)  # fmt: skip
        refs = [first, *[r for r in refs if r.kind != "set"]][:MAX_REFERENCES]
    elif not refs:
        raise NotReady(f"{shot_id} has no design images to draw from")
    draws = max(1, min(draws, MAX_DRAWS))
    mine = sorted((t for t in takes if t.shot_id == shot_id and t.frame == frame),
                  key=lambda t: t.created_at)  # fmt: skip
    last = mine[-1] if mine else None
    fixes = _fixes(last) if last is not None and direction is None and prompt is None else []
    user_prompt = (prompt or "").strip()
    if user_prompt:
        if len(user_prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"the prompt is longer than {MAX_PROMPT_CHARS} characters")
        if over := cited_beyond(user_prompt, len(refs)):
            raise ValueError(f"the prompt cites {', '.join(over)}, which this shot doesn't send")
    take_id = f"stk_{uuid.uuid4().hex[:12]}"
    return ShotTake(
        take_id=take_id,
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        shot_id=shot_id,
        frame=frame,
        run_id=take_id,
        prompt=user_prompt,
        prompt_source="user" if user_prompt else "pending",
        references=refs,
        size=frame_size(pkg),
        draws=draws,
        seeds=_seeds(seeds, draws),
        direction=(direction or "").strip() or None,
        previous_prompt=last.prompt if last is not None and last.prompt else None,
        fixes=fixes,
        # A person's own prompt is theirs to revise: no automatic rewrite of it.
        auto=auto and not user_prompt,
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
You write MiniMax-H3-Image edit prompts for the keyframes of a short drama: one picture per \
shot that the video model will start (or end) the shot from. The picture puts the shot's \
people, exactly as they look in their reference pictures, into the set, framed and posed as \
the storyboard says. Character consistency matters most: every face, hairstyle and costume \
must match its picture. Our server renders the prompt exactly as written (no prompt \
expansion), so the prompt is the finished description. Follow the H3 rules and examples \
below.\
"""

WRITER_GUIDE = """\
## Your input

- `frame`: "start" (the first frame of the shot) or "end" (its last frame; <Picture 1> is \
the approved start frame, and the end frame must be the same shot moments later).
- `pictures`: one entry per <Picture N>, in order, with its role. They are attached.
- `shot`: framing, camera, action, continuity and notes from the storyboard; `people` lists \
who is on screen (exactly these, no one else) and `dialogue` who speaks in the shot.
- `scene`: where and when; `style`: medium, era, color and light; `aspect`: the canvas.
- `previous_prompt` and `fixes`: the last round's prompt and what the quality check found \
wrong with its best draw. Keep what worked and fix every finding.
- `direction`: the user's steer, if any. It wins over everything but the content rules.

## How to write it

One paragraph in MiniMax's edit form:
1. Define each picture: "The woman in <Picture 1> is <Subject 1>." (a person by what the \
picture shows; the set as "The room in <Picture 3>"; a prop by what it is).
2. "The target image is ...": a still frame from a live-action short drama, the shot size \
and camera angle, then the layout: exactly how many people, who is screen-left, \
screen-right, foreground, background, and their body orientation to the camera.
3. One relationship per subject: a person's look picture is partially preserved (face, \
hair, build and every costume detail remain; pose, position and expression follow this \
description); a face picture supplies the exact facial features of that person; the set \
is partially preserved (layout, materials, furniture, palette remain; the camera position \
and the light of the moment follow this description); a prop is fully preserved or \
partially preserved in the state the shot needs. For an end frame, <Subject 1> (the start \
frame) is partially preserved: the same camera, set, light, people and clothes; only the \
poses and positions change, to where the action leaves them.
4. The frozen moment: each person's pose, which hand holds or touches what, gaze, \
expression. For the start frame, the moment the action begins; for the end frame, where \
it ends. A speaking person has their mouth slightly open.
5. Light (source and direction) and lens feel, then "No other lettering is added."

Never add people the shot doesn't list. Describe visible things, not story. Stay under \
3,500 characters.

## What may be drawn

{rules}

{mode}

Keyframes are production references. In a scene with intimacy, show only the clothed \
staging the scene allows (`scene.intimacy.on_screen`), never undressing, nudity or a bed \
scene with bodies under covers.

## Output

Return {{"prompt": "...", "motion": "...", "end_frame": true|false, "notes": "..."}}:
- `prompt`: the prompt only, as plain text.
- `motion`: one sentence on how the shot moves from its first to its last frame.
- `end_frame`: true when the shot's people or camera end somewhere clearly different from \
where they start (an entrance, a turn, a hand-off, a move across the room), so an end \
frame would help the video model; false for a held moment.
- `notes`: one short sentence to the user on what will be hard to get right, or "".\
"""


class FramePromptOutput(BaseModel):
    prompt: str
    motion: str = ""
    end_frame: bool = False
    notes: str = ""


def writer_instructions() -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    guide = WRITER_GUIDE.format(rules=CONTENT_RULES, mode=mode_rule(True))
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


def template_prompt(pkg: ProductionPackage, take: ShotTake) -> str:
    """A usable prompt built by the program (offline, or when the writer fails)."""
    shot = shot_of(pkg, take.shot_id)
    names = _names(pkg)
    defs, rels = [], []
    for n, r in enumerate(take.references, 1):
        who = names.get(r.entity_id or "", "")
        subject = f"<Subject {n}>"
        match r.kind:
            case "start_frame":
                defs.append(f"The film frame in <Picture {n}> is {subject}.")
                rels.append(f"{subject} is partially preserved: the camera, set, light, people "
                            "and clothing remain; only the poses and positions change, to "
                            "where the action ends.")  # fmt: skip
            case "look":
                defs.append(f"The person in <Picture {n}> is {subject} ({who}).")
                rels.append(f"{subject} is partially preserved: the face, hair, build and every "
                            "costume detail remain; pose, position and expression follow this "
                            "description.")  # fmt: skip
            case "face":
                defs.append(f"The face in <Picture {n}> is {subject}.")
                rels.append(f"{subject} supplies the exact facial features of {who}.")
            case "set":
                defs.append(f"The room in <Picture {n}> is {subject}.")
                rels.append(f"{subject} is partially preserved: the layout, materials, "
                            "furniture and palette remain; the camera position and light "
                            "follow this description.")  # fmt: skip
            case "prop":
                defs.append(f"The object in <Picture {n}> is {subject} ({who}).")
                rels.append(f"{subject} is fully preserved.")
    count = len(shot.character_ids)
    people = (f"exactly {count} {'person' if count == 1 else 'people'}" if count
              else "no people")  # fmt: skip
    moment = "where the action ends" if take.frame == "end" else "the moment the action begins"
    target = (f"The target image is a still frame from a live-action short drama: "
              f"{shot.framing.rstrip('.')}, {shot.camera.rstrip('.')}, showing {people}, at "
              f"{moment}: {shot.visual_action.rstrip('.')}.")  # fmt: skip
    extra = [take.direction.rstrip(".") + "." if take.direction else ""]
    return " ".join(p for p in [*defs, target, *rels, *extra, NO_OTHER_TEXT] if p)


def fixture_frame_prompt(request: GenerationRequest) -> FramePromptOutput:
    """Offline stand-in for the writer: the template, passed in the context."""
    return FramePromptOutput(
        prompt=request.context.get("template_prompt", ""),
        motion="Offline sample: the action as the storyboard describes it.",
        end_frame=False,
        notes="Offline sample: the built-in template.",
    )


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
- `shot`: what the frame must show: framing, camera, action, `people` (exactly these, \
`people_count` of them), props and dialogue.
- `frame`: "start" (the moment the action begins) or "end" (where it ends; <Picture 1> is \
then the approved start frame, and the camera, set, light and clothes must match it).
- `prompt`: the prompt the frame was drawn from (for context; judge the frame, not the prompt).

## Check these items

- For each person in `shot.people`: one item each for **identity** (face shape, features, \
skin, age: the same person as their picture) and **wardrobe** (clothes, hair, accessories \
as in their look picture). Name them: "Lila: face", "Lila: wardrobe".
- **count**: exactly `people_count` people with visible faces or bodies, no extras \
(small background silhouettes are fine if the shot implies them).
- **placement**: who is where (screen-left/right, foreground/background) and facing which \
way, as the framing and action say.
- **framing**: the shot size and camera angle.
- **action**: the pose and the moment match the action (and the frame: start or end).
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
    shot = request.context.get("shot", {})
    items = [
        CheckItem(item=f"{p['name']}: {what}", kind=kind, passed=True)
        for p in shot.get("people", [])
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
        self._tasks: set[asyncio.Task[None]] = set()

    def recover(self) -> int:
        return self.repo.fail_unfinished_shot_takes(
            "Interrupted by a server restart. Retry to run it again."
        )

    def start(self, take_id: str) -> None:
        task = asyncio.create_task(self.run(take_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        template = template_prompt(pkg, take)
        media = self._media(take)
        request = GenerationRequest(
            stage=WRITE_STAGE,  # type: ignore[arg-type]
            instructions=writer_instructions(),
            context={
                "frame": take.frame,
                "pictures": [
                    {"picture": f"<Picture {n}>", "role": r.role}
                    for n, r in enumerate(take.references, 1)
                ],
                **shot_context(pkg, shot),
                "aspect": next((k for k, v in SIZES.items() if v == take.size), take.size),
                "previous_prompt": take.previous_prompt,
                "fixes": take.fixes,
                "direction": take.direction,
                "template_prompt": template,
            },
            media=media,
        )
        try:
            out = await self.text.generate(request, FramePromptOutput)
            prompt = out.prompt.strip()
            problem = (
                "the writer returned an empty prompt" if not prompt
                else f"the prompt cites {', '.join(over)}, which this shot doesn't send"
                if (over := cited_beyond(prompt, len(take.references)))
                else f"the prompt is longer than {MAX_PROMPT_CHARS} characters"
                if len(prompt) > MAX_PROMPT_CHARS
                else None
            )  # fmt: skip
            if problem:
                raise ValueError(problem)
        except Exception as exc:
            log.warning("shot frame writer failed for %s", take.take_id, exc_info=True)
            take.prompt, take.prompt_source = template, "template"
            take.notes = (
                f"The prompt writer failed ({str(exc)[:120]}); using the built-in template."
            )
            return
        take.prompt, take.prompt_source = prompt, "model"
        take.prompt_model = request.model or getattr(self.text, "model_id", None)
        take.notes = out.notes.strip()
        if take.frame == "start":
            take.motion, take.end_frame = out.motion.strip(), out.end_frame
        self.repo.save_shot_take(take)

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
            "frame": take.frame,
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
    pkg: ProductionPackage, images: list[GeneratedImage], takes: list[ShotTake]
) -> list[dict[str, Any]]:
    """Every shot with what it draws from, what is missing, and its takes per frame."""
    names = _names(pkg)
    out = []
    for shot in pkg.shot_list:
        refs, missing = shot_references(pkg, images, shot)
        mine = sorted((t for t in takes if t.shot_id == shot.shot_id), key=lambda t: t.created_at)
        start = [t for t in mine if t.frame == "start"]
        suggested = next((t.end_frame for t in reversed(start) if t.end_frame is not None), None)
        out.append({
            "shot_id": shot.shot_id,
            "episode_id": shot.episode_id,
            "scene_id": shot.scene_id,
            "framing": shot.framing,
            "camera": shot.camera,
            "action": shot.visual_action,
            "duration_ms": shot.duration_ms,
            "people": [names.get(c, c) for c in shot.character_ids],
            "place": names.get(shot.location_id, shot.location_id),
            "intimacy": intimate_scene(pkg, shot),
            "references": [r.model_dump() for r in refs],
            "missing": missing,
            "end_suggested": suggested,
            "takes": [t.model_dump(mode="json") for t in mine],
            "approved": {f: (a.take_id, a.approved) if (a := approved_take(mine, shot.shot_id, f))
                         else None for f in ("start", "end")},
        })  # fmt: skip
    return out
