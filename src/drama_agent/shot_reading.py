"""Choosing each keyframe's reference pictures by reading the scene.

A character has a handful of design pictures (a face, a look per costume), and the
storyboard names one look per shot. That look is the intended outfit, not what the camera
sees at that moment: in a morning-after scene the "work" look comes with pinned hair and a
work bag while Lila is still pulling on her trousers. So the reference pictures are chosen
the way a script supervisor would:

1. **The target state.** For every person on screen in every sub-shot of a scene, what they
   wear, how their hair is, what they hold and their condition at that moment, reasoned in
   order through the scene (state carries over unless the action changes it).
2. **The closest picture.** Of that person's design pictures, the one that needs the least
   changed to reach the state (clothing matters most, then hair; what is held and the
   condition are easy to change in words), with what it gets right (`keep`) and why.
3. **What differs is said in the prompt.** The picture's sentence keeps the face, build and
   `keep`, and states the target (shot_frames.picture_sentences), so a less-than-perfect
   picture is safe: the bag is not carried unless the state says so.

One text call per scene (stage "shot_reading", gpt-6-luna by default, config.py) reads the
scene's shots and sub-shots with every candidate picture attached. A person can override
any choice; overrides survive a new reading as long as the picture is still a candidate.
A reading is fresh while the scene's plans and candidate pictures are what it read
(`fingerprint`); a stale one is read again before the next written take.

The same reading says which part of the set is behind each cut. A standard set has a
mid-shot picture of each of its most used areas (the set sheet's `areas`); a close shot is
drawn from the picture of the area it plays in, and a wide shot from the set's wide view
(`set_views`, read by shots/references.py).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .images import selected_image
from .llm import GenerationRequest
from .models import ProductionPackage
from .models.images import GeneratedImage
from .models.package import utcnow
from .models.storyboard import Shot

STAGE = "shot_reading"


class PersonState(BaseModel):
    """What a person looks like at one moment, in plain visual words."""

    wearing: str = ""  # garments and shoes (or bare feet), as they are now
    hair: str = ""
    carrying: str = ""  # what is in their hands or on them; "" for nothing
    condition: str = ""  # wet, tired, a cut on the cheek, clean...

    def sentence(self, who: str) -> str:
        """The state as one clause for the prompt: "Lila wears ..., her hair is ..."."""
        parts = [
            f"{who} wears {self.wearing.rstrip('.')}" if self.wearing else "",
            f"hair {self.hair.rstrip('.')}" if self.hair else "",
            f"holding {self.carrying.rstrip('.')}" if self.carrying else "with empty hands",
            self.condition.rstrip(".") if self.condition else "",
        ]
        return ", ".join(p for p in parts if p)


class Choice(BaseModel):
    """One person in one sub-shot: their state, and the design picture they are drawn from."""

    shot_id: str
    sub_shot: int
    character_id: str
    state: PersonState
    image_id: str
    keep: list[str] = Field(default_factory=list)  # what the picture already gets right
    reason: str = ""
    source: Literal["model", "user", "storyboard"] = "model"


class Candidate(BaseModel):
    """A design picture a person may be drawn from."""

    image_id: str
    character_id: str
    kind: Literal["look", "face"]
    label: str  # "work look, full body"
    look_id: str | None = None
    likeness: bool = False
    shows: str = ""  # what the reading saw in it


class SceneReading(BaseModel):
    package_id: str
    revision_id: str
    scene_id: str
    status: Literal["writing", "done", "failed"] = "done"
    fingerprint: str = ""
    # Per cut ("shot_id:sub_shot"), a print of the cut as read: a new reading keeps the
    # choices of the cuts that haven't changed (see carry_unchanged).
    cuts: dict[str, str] = Field(default_factory=dict)
    candidates: list[Candidate] = Field(default_factory=list)
    choices: list[Choice] = Field(default_factory=list)
    # Per cut ("shot_id:sub_shot"), the area of the set behind it, numbered from 1 as the
    # set sheet lists them; 0 (or no entry) is the set's wide view.
    set_views: dict[str, int] = Field(default_factory=dict)
    model: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def busy(self) -> bool:
        return self.status == "writing"

    def set_view(self, shot_id: str, sub_shot: int) -> int:
        return self.set_views.get(f"{shot_id}:{sub_shot}", 0)

    def choice(self, shot_id: str, sub_shot: int, character_id: str) -> Choice | None:
        return next(
            (c for c in self.choices
             if (c.shot_id, c.sub_shot, c.character_id) == (shot_id, sub_shot, character_id)),
            None,
        )  # fmt: skip


# ---------------------------------------------------------------------------
# What a scene is made of


def scene_shots(pkg: ProductionPackage, scene_id: str) -> list[Shot]:
    return sorted((s for s in pkg.shot_list if s.scene_id == scene_id), key=lambda s: s.start_ms)


def set_areas(pkg: ProductionPackage, scene_id: str) -> list[Any]:
    """The areas of the scene's set, as its sheet lists them (SetArea); [] for none."""
    shots = scene_shots(pkg, scene_id)
    if not shots or pkg.set_sheet is None:
        return []
    loc = next((x for x in pkg.set_sheet.locations if x.location_id == shots[0].location_id),
               None)  # fmt: skip
    return list(loc.areas) if loc is not None else []


def candidates(
    pkg: ProductionPackage, images: list[GeneratedImage], character_id: str
) -> list[Candidate]:
    """A character's design pictures: the pick of each look (full body) and of the face."""
    looks = {lk.look_id: lk for lk in (pkg.cast_sheet.looks if pkg.cast_sheet else [])}
    variants = {lk.look_id: lk.description for lk in pkg.story_bible.looks}
    out: list[Candidate] = []
    for req in pkg.asset_requirements.items:
        if not req.subject_refs or req.subject_refs[0].entity_id != character_id:
            continue
        if req.asset_type not in ("character_look", "character_identity"):
            continue
        view = "full body" if req.asset_type == "character_look" else "front face"
        d = next((d for d in req.deliverables if d.view == view), None)
        img = d and selected_image(images, (req.requirement_id, d.deliverable_id))
        if img is None:
            continue
        look_id = req.subject_refs[0].variant_id
        if req.asset_type == "character_look":
            words = variants.get(look_id or "") or (
                looks[look_id].wardrobe if look_id in looks else ""
            )
            label = f"look {look_id or ''}: {words[:120]}".strip()
            out.append(Candidate(image_id=img.image_id, character_id=character_id, kind="look",
                                 label=label, look_id=look_id, likeness=img.likeness))  # fmt: skip
        else:
            out.append(Candidate(image_id=img.image_id, character_id=character_id, kind="face",
                                 label="face (identity portrait)", likeness=img.likeness))  # fmt: skip
    # Looks first (they show the clothes), then the face.
    return sorted(out, key=lambda c: c.kind != "look")


def fingerprint(
    pkg: ProductionPackage, scene_id: str, plans: dict[str, Any], images: list[GeneratedImage]
) -> str:
    """What a reading of the scene depends on: its shots' sub-shots and the pictures to choose
    from. A reading whose fingerprint differs is stale."""
    from .shot_frames import plan_of

    shots = scene_shots(pkg, scene_id)
    people = sorted({c for s in shots for c in s.character_ids})
    data = {
        "shots": [
            [s.shot_id, [sub.model_dump(exclude={"placement", "off_screen"})
                         for sub in plan_of(pkg, s, plans.get(s.shot_id)).sub_shots]]
            for s in shots
        ],
        "pictures": [c.image_id for p in people for c in candidates(pkg, images, p)],
        # The looks it reads (a hand edit to one makes the reading stale).
        "looks": [[lk.model_dump() for lk in (pkg.cast_sheet.looks if pkg.cast_sheet else [])
                   if lk.character_id in people],
                  [lk.model_dump() for lk in pkg.story_bible.looks if lk.character_id in people]],
    }  # fmt: skip
    # The set's areas, when it lists any (a set without them keeps the print it had).
    if areas := set_areas(pkg, scene_id):
        data["areas"] = [a.model_dump() for a in areas]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def cut_prints(pkg: ProductionPackage, scene_id: str, plans: dict[str, Any]) -> dict[str, str]:
    """Per cut of the scene ("shot_id:sub_shot"), a print of what the reading sees of it:
    the cut's view, action, timing and people. Same print, same cut."""
    from .shot_frames import plan_of

    out: dict[str, str] = {}
    for s in scene_shots(pkg, scene_id):
        for sub in plan_of(pkg, s, plans.get(s.shot_id)).sub_shots:
            raw = json.dumps(sub.model_dump(exclude={"placement", "off_screen"}), sort_keys=True,
                             default=str)  # fmt: skip
            out[f"{s.shot_id}:{sub.index}"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return out


def carry_unchanged(
    new: list[Choice], old: SceneReading | None, cuts: dict[str, str], pool: list[Candidate]
) -> list[Choice]:
    """A new reading keeps the old one's choices for every cut that is the same as when it
    was read (`cuts`: cut_prints now), where the person still has that picture to choose
    (or the picture that replaced it as the pick of the same look).
    A plan change in one shot then leaves the scene's other shots as they were: their
    keyframes, prompts and clips stay current instead of being redone from a reading
    that only differs in wording."""
    if old is None or not old.cuts:
        return new
    ids = {(c.character_id, c.image_id) for c in pool}
    # A look or face whose pick was replaced is the same choice with the new picture: the
    # moment still calls for that look, and what the person wears then hasn't changed.
    slot = {(c.character_id, c.kind, c.look_id): c.image_id for c in pool}
    now = {(c.character_id, c.image_id): slot.get((c.character_id, c.kind, c.look_id))
           for c in old.candidates}  # fmt: skip
    out = []
    for c in new:
        key = f"{c.shot_id}:{c.sub_shot}"
        before = old.choice(c.shot_id, c.sub_shot, c.character_id)
        if before is not None and old.cuts.get(key) == cuts.get(key):
            if (c.character_id, before.image_id) in ids:
                c = before.model_copy()
            elif replaced := now.get((c.character_id, before.image_id)):
                c = before.model_copy(update={"image_id": replaced})
        out.append(c)
    return out


def fresh(reading: SceneReading | None, print_: str) -> SceneReading | None:
    """The reading if it is done and read the scene as it is now."""
    if reading is None or reading.status != "done" or reading.fingerprint != print_:
        return None
    return reading


# ---------------------------------------------------------------------------
# The reading (one text call per scene)

ROLE = """\
You are the script supervisor and costume continuity lead of a short drama shot with an AI \
image model. For one scene, you decide what each person looks like at each moment the \
camera shows them, and which of their few design pictures each keyframe should be drawn \
from. The image model copies what a picture shows unless the prompt says otherwise, so the \
closest picture makes the best frame.\
"""

GUIDE = """\
## Your input

- `scene`: where and when, and its staging when it has intimacy. `scene.areas`, when \
present, lists the parts of the set that have their own reference picture: each with its \
number, name and what that picture shows.
- `shots`: the scene's shots in order. Each has its storyboard `look` per person (the \
outfit intended for the shot; it may not fit the moment, e.g. a work look with a bag while \
someone is still dressing), its framing, action, lines, and its `sub_shots` (the cuts inside \
the shot's clip), each with `people` on screen.
- `people`: each person in the scene with their looks (the costume design notes).
- `pictures`: every candidate design picture, attached in order after its label \
(`<Image 1>`, ...), each with whose it is and what it is meant to be.

## What to decide

1. For every picture, `shows`: what it actually shows, in visual words (garments, shoes or \
bare feet, hair, anything held, condition).
2. For every person on screen in every sub-shot, in story order, their state at the moment \
that sub-shot begins: `wearing` (garments as worn now, shoes or bare feet), `hair`, \
`carrying` (what is in their hands or on them; "" for nothing), `condition`. State carries \
over from the moment before unless the action changes it (she pulls on her trousers; he \
picks up the bag). Use the storyboard look and the costume notes as the intended outfit, \
but fit them to the moment and keep continuity within the scene.
3. For each of those, the closest picture of that person (`image`): the one that needs the \
least changed to show the state. Clothing matters most, then hair; what is held, shoes and \
the condition are easy to change in words. Prefer a full-body look picture over the face; \
for continuity, prefer the picture already chosen for that person earlier in the scene when \
it fits as well. `keep`: what the picture already gets right (e.g. "ivory blouse", \
"charcoal trousers", "loose hair"). `reason`: one short sentence.

4. When `scene.areas` is present, for every sub-shot, the `area` behind the people in that \
framing: the number of the area whose picture shows the background this framing describes \
(what is behind and beside whoever is on screen, from that side of the room). Give 0 when \
the framing is wider than one area (an establishing shot, a shot across the room), when it \
is an insert that shows no background, or when no area fits: the set's wide view is used \
then. Go by the framing's own words about the background and where people stand, not by \
which area the scene mostly plays in.

Describe visible things only. Return {"pictures": [{"image": "<Image 1>", "shows": "..."}], \
"states": [{"shot_id": "...", "sub_shot": 1, "person": "<name>", "wearing": "...", "hair": \
"...", "carrying": "", "condition": "...", "image": "<Image 2>", "keep": ["..."], "reason": \
"..."}], "views": [{"shot_id": "...", "sub_shot": 1, "area": 2}]}, with one state per person \
on screen per sub-shot, and one view per sub-shot when the scene lists areas.\
"""


class PictureOut(BaseModel):
    image: str
    shows: str = ""


class StateOut(BaseModel):
    shot_id: str
    sub_shot: int
    person: str
    wearing: str = ""
    hair: str = ""
    carrying: str = ""
    condition: str = ""
    image: str
    keep: list[str] = Field(default_factory=list)
    reason: str = ""


class ViewOut(BaseModel):
    shot_id: str
    sub_shot: int
    area: int = 0


class ReadingOutput(BaseModel):
    pictures: list[PictureOut] = Field(default_factory=list)
    states: list[StateOut] = Field(default_factory=list)
    views: list[ViewOut] = Field(default_factory=list)


def instructions() -> str:
    return ROLE + "\n\n" + GUIDE


def reading_context(
    pkg: ProductionPackage, scene_id: str, plans: dict[str, Any], pool: list[Candidate]
) -> dict[str, Any]:
    from .shot_frames import _names, plan_of, scene_of, shot_lines

    names = _names(pkg)
    shots = scene_shots(pkg, scene_id)
    sc = scene_of(pkg, shots[0]) if shots else None
    looks = {lk.look_id: lk for lk in (pkg.cast_sheet.looks if pkg.cast_sheet else [])}
    people = sorted({c for s in shots for c in s.character_ids}, key=lambda c: names.get(c, c))
    scene: dict[str, Any] = {"place": names.get(shots[0].location_id, "") if shots else ""}
    if areas := set_areas(pkg, scene_id):
        scene["areas"] = [{"area": i, "name": a.name, "shows": a.view}
                          for i, a in enumerate(areas, 1)]  # fmt: skip
    if sc is not None:
        scene |= {"int_ext": sc.int_ext, "time_of_day": sc.time_of_day}
        if sc.intimacy is not None and sc.intimacy.level != "none":
            scene["intimacy"] = {"level": sc.intimacy.level, "on_screen": sc.intimacy.on_screen}
    return {
        "scene": scene,
        "shots": [
            {
                "shot_id": s.shot_id,
                "look": {names.get(r.character_id, r.character_id): r.look_id
                         for r in s.character_look_refs},
                "framing": s.framing,
                "action": s.visual_action,
                "continuity": s.continuity,
                "lines": [f"{x['speaker']}: {x['text']}" for x in shot_lines(pkg, s)],
                "sub_shots": [
                    {"sub_shot": sub.index, "start_s": sub.start_ms / 1000,
                     "framing": sub.framing, "action": sub.action,
                     "people": [names.get(c, c) for c in sub.people]}
                    for sub in plan_of(pkg, s, plans.get(s.shot_id)).sub_shots
                ],
            }
            for s in shots
        ],
        "people": [
            {"name": names.get(c, c),
             "looks": [{"look": lk.look_id, "wardrobe": lk.wardrobe,
                        "hair_and_makeup": lk.hair_and_makeup, "condition": lk.condition}
                       for lk in looks.values() if lk.character_id == c]}
            for c in people
        ],
        "pictures": [
            {"image": f"<Image {i}>", "person": names.get(c.character_id, c.character_id),
             "meant_as": c.label}
            for i, c in enumerate(pool, 1)
        ],
    }  # fmt: skip


def read_output(
    pkg: ProductionPackage,
    scene_id: str,
    plans: dict[str, Any],
    pool: list[Candidate],
    out: ReadingOutput,
) -> tuple[list[Candidate], list[Choice]]:
    """The reading as candidates (with what each shows) and choices, checked: a state for
    every person on screen in every sub-shot, each drawn from one of that person's own
    pictures. Raises ValueError listing what is wrong."""
    from .shot_frames import _who, plan_of

    by_label = {f"<Image {i}>": c for i, c in enumerate(pool, 1)}
    pool = [c.model_copy() for c in pool]
    for p in out.pictures:
        if (c := by_label.get(p.image.strip())) is not None:
            next(x for x in pool if x.image_id == c.image_id).shows = p.shows.strip()
    problems: list[str] = []
    choices: list[Choice] = []
    shots = {s.shot_id: s for s in scene_shots(pkg, scene_id)}
    for st in out.states:
        shot = shots.get(st.shot_id)
        if shot is None:
            problems.append(f"{st.shot_id} is not a shot of this scene")
            continue
        try:
            who = _who(pkg, shot, st.person)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        sub = plan_of(pkg, shot, plans.get(shot.shot_id)).sub(st.sub_shot)
        if sub is None:
            problems.append(f"{st.shot_id} has no sub-shot {st.sub_shot}")
            continue
        if who not in sub.people:
            problems.append(f"{st.person} is not on screen in {st.shot_id} sub-shot {st.sub_shot}")
            continue
        pic = by_label.get(st.image.strip())
        if pic is None or pic.character_id != who:
            problems.append(f"{st.image} is not a picture of {st.person}")
            continue
        choices.append(Choice(
            shot_id=st.shot_id, sub_shot=st.sub_shot, character_id=who,
            state=PersonState(wearing=st.wearing.strip(), hair=st.hair.strip(),
                              carrying=st.carrying.strip(), condition=st.condition.strip()),
            image_id=pic.image_id, keep=[k.strip() for k in st.keep if k.strip()],
            reason=st.reason.strip(),
        ))  # fmt: skip
    for shot in shots.values():
        for sub in plan_of(pkg, shot, plans.get(shot.shot_id)).sub_shots:
            for who in sub.people:
                has_pictures = any(c.character_id == who for c in pool)
                if has_pictures and not any(
                    (c.shot_id, c.sub_shot, c.character_id) == (shot.shot_id, sub.index, who)
                    for c in choices
                ):
                    problems.append(f"no state for {who} in {shot.shot_id} sub-shot {sub.index}")
    if problems:
        raise ValueError("; ".join(problems))
    return pool, choices


def read_views(
    pkg: ProductionPackage, scene_id: str, plans: dict[str, Any], out: ReadingOutput
) -> dict[str, int]:
    """The area behind each cut as the reading chose it: only cuts the scene has, only
    areas the set lists (anything else is the wide view)."""
    from .shot_frames import plan_of

    n = len(set_areas(pkg, scene_id))
    cuts = {f"{s.shot_id}:{sub.index}" for s in scene_shots(pkg, scene_id)
            for sub in plan_of(pkg, s, plans.get(s.shot_id)).sub_shots}  # fmt: skip
    views = {}
    for v in out.views:
        key = f"{v.shot_id}:{v.sub_shot}"
        if key in cuts and 0 < v.area <= n:
            views[key] = v.area
    return views


def merge_overrides(
    new: list[Choice], old: SceneReading | None, pool: list[Candidate]
) -> list[Choice]:
    """A person's own picture choices carry over to a new reading, where the same person in
    the same sub-shot still has that picture to choose."""
    if old is None:
        return new
    ids = {(c.character_id, c.image_id) for c in pool}
    out = []
    for c in new:
        mine = old.choice(c.shot_id, c.sub_shot, c.character_id)
        if mine is not None and mine.source == "user" and (c.character_id, mine.image_id) in ids:
            c = c.model_copy(update={"image_id": mine.image_id, "keep": [], "source": "user",
                                     "reason": mine.reason})  # fmt: skip
        out.append(c)
    return out


def fixture_reading(request: GenerationRequest) -> ReadingOutput:
    """Offline stand-in: every person drawn from their storyboard look's picture (else their
    first picture), in the look's clothes."""
    ctx = request.context
    pics = ctx["pictures"]
    states = []
    for shot in ctx["shots"]:
        for sub in shot["sub_shots"]:
            for person in sub["people"]:
                mine = [p for p in pics if p["person"] == person]
                if not mine:
                    continue
                look = shot["look"].get(person)
                pick = next((p for p in mine if look and look in p["meant_as"]), mine[0])
                facts = next((lk for who in ctx["people"] if who["name"] == person
                              for lk in who["looks"] if lk["look"] == look), {})  # fmt: skip
                states.append(StateOut(shot_id=shot["shot_id"], sub_shot=sub["sub_shot"],
                                       person=person, wearing=facts.get("wardrobe", ""),
                                       hair=facts.get("hair_and_makeup", ""),
                                       image=pick["image"], reason="Offline sample."))  # fmt: skip
    return ReadingOutput(pictures=[PictureOut(image=p["image"], shows="") for p in pics],
                         states=states)  # fmt: skip
