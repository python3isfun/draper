"""The keyframe prompt writer: its guide, the context it reads, picture sentences, the
template prompt and the prompt's rule findings."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ..content_guard import rules as content_rules
from ..experiment import cited_beyond
from ..llm import GenerationRequest
from ..models import ProductionPackage
from ..models.request import DEFAULT_RATING
from ..models.storyboard import Shot
from ..shot_reading import PersonState
from ..validation.hard_checks import shot_size
from .common import _names, scene_of, shot_of
from .models import NO_OTHER_TEXT, FrameRef, ShotTake, SubShot
from .plans import shot_lines, single_plan, sub_shot_lines

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
- `scene`: where and when; `scene.set`: the set sheet, where things are in the room \
(`layout`: which wall the bed, the windows and the doors are on), its furnishings, \
materials, light and dressing at this moment. The set picture shows one view; from any \
other camera direction, put behind each person what `layout` says is on that side \
(e.g. the wall and the canvas behind the headboard, the windows to the side), never what \
an older plan or keyframe says. `style`: medium, era, color and light; `aspect`: the canvas.
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
a scene with intimacy, show the staging `scene.intimacy.on_screen` describes, within the \
story's rating (rule 1): never genitals or a sex act shown explicitly, and below R no nudity \
(no woman's bare breasts); describe what is there, not what is absent. Rules 1 to 3 above \
always apply.\
"""


class FramePromptOutput(BaseModel):
    prompt: str
    motion: str = ""
    notes: str = ""


def writer_instructions(rating: str = DEFAULT_RATING) -> str:
    from ..prompts import load_h3_image_examples, load_h3_image_rules

    guide = WRITER_GUIDE.format(rules=content_rules(rating), mode=KEYFRAME_RULE)
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
    # Where things are in the room (the set sheet), so every camera direction puts the right
    # wall, window or door behind whom; and the state's light and dressing.
    sheet = pkg.set_sheet
    loc = next((x for x in (sheet.locations if sheet else []) if x.location_id == shot.location_id),
               None)  # fmt: skip
    st = next((x for x in (sheet.states if sheet else [])
               if x.location_state_id == shot.location_state_id), None)  # fmt: skip
    if loc is not None or st is not None:
        scene["set"] = {k: v for k, v in {
            "layout": getattr(loc, "layout", ""),
            "furnishings": getattr(loc, "key_furnishings", ""),
            "materials": getattr(loc, "materials_and_textures", ""),
            "lighting": getattr(st, "lighting", "") or getattr(loc, "lighting", ""),
            "dressing": getattr(st, "dressing_changes", ""),
        }.items() if v}  # fmt: skip
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


# What a close framing leaves below its lower edge. H3 draws what a prompt names: a medium
# close-up whose person "wears black trousers and flat loafers" comes out waist-down wide
# (measured: reports/keyframe-person-sentence.md). So a close framing's person sentence
# names only what it shows.
_BELOW_FRAME = re.compile(
    r"\b(trousers?|pants|jeans|slacks|leggings|shorts|skirt|hem|shoes?|pumps?|heels?|loafers?|"
    r"boots?|sneakers?|sandals?|flats|socks?|stockings?|tights|barefoot|bare[- ]feet|feet|foot|"
    r"legs?|ankles?|knees?|waistband|belt)\b",
    re.IGNORECASE,
)
_JOINS = re.compile(r"\s+(?:tucked into|worn over|over|above|around|with|and)\s+", re.IGNORECASE)
_CLOSE_SIZES = ("close-up", "medium close-up")
_OTS = re.compile(r"\bover[- ]the[- ]shoulder\b|\bover\b[^.,;]*\bshoulder\b|\bOTS\b", re.IGNORECASE)


# What a state says is out of the picture ("joined with Ryder's below frame"): named, it is
# drawn, at any shot size.
_UNSEEN = re.compile(
    r"\b(?:below|beneath|under|outside|out of|beyond|off)[- ](?:the |this )?(?:frame|crop|shot|"
    r"screen|camera)\b|\boff[- ]?screen\b|\boff[- ]?camera\b|\bunseen\b|\bnot visible\b",
    re.IGNORECASE,
)


def seen(text: str) -> str:
    """`text` without the clauses about what the frame doesn't show."""
    parts = re.split(r"\s*(?:[;,]|\bwhile\b|\bas\b)\s*", text)
    return ", ".join(p.strip() for p in parts if p.strip() and not _UNSEEN.search(p))


def close_framing(framing: str) -> bool:
    """Whether a framing ends at the chest or above: a close-up, a medium close-up, or an
    over-the-shoulder shot that names no wider size."""
    if re.search(r"\binsert\b|\bdetail\b", framing, re.IGNORECASE):
        return False  # an insert of hands or feet shows exactly what it names
    size = shot_size(framing)
    return size in _CLOSE_SIZES or (size is None and bool(_OTS.search(framing)))


def in_frame(text: str) -> str:
    """`text` (garments, or a condition) without what a close framing doesn't show: "ivory
    blouse tucked into charcoal trousers; barefoot" is "ivory blouse"."""
    kept = []
    for part in re.split(r"\s*[;,]\s*", text):
        part = re.sub(r"^(?:and|with)\s+", "", part.strip(), flags=re.IGNORECASE)
        head = []
        for piece in _JOINS.split(part):
            if _BELOW_FRAME.search(piece):
                break  # what follows hangs on it ("trousers with a pressed crease")
            head.append(piece)
        if head and (shown := part[: part.index(head[-1]) + len(head[-1])].strip()):
            kept.append(shown)
    return ", ".join(kept)


def picture_sentences(pkg: ProductionPackage, take: ShotTake) -> list[dict[str, Any]]:
    """Per <Picture N>: its role, and the definition and relationship sentences the prompt
    uses for it (MiniMax's edit form). Written here, not by the model, so every picture is
    used for what it is: a face picture always transfers that exact face, and the person's look
    picture sent with it keeps the build and the clothes, not its own face. A person's picture
    chosen by reading the scene keeps the face, build and what it already gets right, and
    states how they look at this moment (shot_reading.py), so what the picture shows that
    the moment doesn't (a bag in hand, pinned hair) is not carried over. Without a reading,
    the garments carry over but things held only appear where the action puts them. In a
    close framing the state names only what the frame shows: trousers and shoes named in a
    medium close-up are drawn, and the frame widens to fit them."""
    names = _names(pkg)
    close = close_framing(take_sub(pkg, shot_of(pkg, take.shot_id), take).framing)

    # A person's look picture is sent with their face picture when there is one. The look
    # picture has a face of its own, small and never quite the same person: named among
    # what it keeps, H3 draws that face, or a blend, and the person changes from keyframe
    # to keyframe. With a face picture the look keeps the build and the clothes only.
    face_of = {r.entity_id: n for n, r in enumerate(take.references, 1) if r.kind == "face"}

    def to_state(subject: str, who: str, r: FrameRef) -> str:
        assert r.state is not None
        faced = r.kind == "look" and r.entity_id in face_of
        face = f"the build (the face is <Subject {face_of[r.entity_id]}>'s)" if faced else ""
        keep = [k for k in (seen(k) for k in r.keep) if k]
        state = r.state.model_copy(update={"wearing": seen(r.state.wearing),
                                           "carrying": seen(r.state.carrying),
                                           "condition": seen(r.state.condition)})  # fmt: skip
        if close:
            # A frame that ends at the chest: the face, the hair and the upper garment. What
            # is held and the person's condition are mostly hands, bags and what happens
            # below the crop; the description places whatever of them the frame shows.
            wearing, hair = in_frame(state.wearing), state.hair.rstrip(".")
            now = ", ".join(x for x in (f"{who or 'the person'} wears {wearing}" if wearing else "",
                                        f"hair {hair}" if hair else "") if x)  # fmt: skip
            return (f"{subject} is partially preserved: {face or 'the face, build'} and the "
                    f"garments this framing shows remain; {now + '; ' if now else ''}anything held in the "
                    "picture appears only where the action puts it; the pose, position and "
                    "expression follow this description.")  # fmt: skip
        kept = ", ".join([face] if face else ["the face", "build"]) + "".join(
            f", {k}" for k in keep
        )
        return (f"{subject} is partially preserved: {kept} remain; "
                f"{state.sentence(who or 'the person')}; the pose, position and expression "
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
                kept = (f"the hair, build (the face is <Subject {face_of[r.entity_id]}>'s)"
                        if r.entity_id in face_of else "the face, hair, build")  # fmt: skip
                rel = (f"{subject} is partially preserved: {kept} and every "
                       "garment remain; anything held in the picture appears only where the "
                       "action puts it; the pose, position and expression follow this "
                       "description.")  # fmt: skip
            case "face" if r.entity_id in look_of:
                scope = f"{who}'s face" if who else "The face"
                define = f"{scope} in <Picture {n}> is {subject}."
                rel = (f"{subject} is the face of <Subject {look_of[r.entity_id]}> in the "
                       "image: its exact facial features, face shape and apparent age, in "
                       f"place of the face <Picture {look_of[r.entity_id]}> shows.")  # fmt: skip
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


def prompt_findings(
    prompt: str, refs: list[FrameRef], fixed: tuple[str, ...] = (), rating: str = DEFAULT_RATING
) -> list[str]:
    """Where a keyframe prompt breaks the H3 image rules, as far as a program can tell:
    the edit form (every picture defined, the overview, one relationship each, the closing
    sentence), negations, instructions, story words, and a face picture used as anything
    but the face. The wording checks skip the `fixed` sentences the program wrote (a
    person's state quotes the costume notes, which may say "not")."""
    from ..image_prompts import problems

    out = [
        f"cites {', '.join(over)}, which this sub-shot doesn't send (it sends {len(refs)})"
        if f.startswith("cites a picture") and (over := cited_beyond(prompt, len(refs)))
        else f
        for f in problems(prompt, len(refs), rating)
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
