"""Face Lab, and the same study for every design picture (docs/library-layers.md): turn an
uploaded photo into the most descriptive prompt for that kind of picture (a face, a look's
full body, a set, a prop), then test the prompt with chosen draws and seeds.

A Face Lab study is an Experiment session of kind "face", so it shares storage, image
numbers, the content guard, likeness tracking, retries and the GPU queue:

- a **describe** turn (mode "describe") sends the photo to the text model, which writes a
  text-to-image prompt recreating the person as closely as text allows. Nothing is drawn.
- a **test** turn (mode "exact", no pictures) renders a prompt from text alone, with fixed
  seeds when given, so two wordings can be compared seed for seed.

A test inherits the photo's likeness: if the photo shows an identifiable person, the
renders are meant to look like them, so content rule 3 applies to them.

A study's `subject` comes from the picture it is for. A face, a set's first state and a
prop's first state are drawn from text alone. A picture drawn from another one (a look
from the face, a later set or prop state from the first, a top-down plan from its wide
view) is tested the same way: with that picture's current pick as `<Picture 1>` (the
study's anchor), so a look keeps the character's face.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel

from .content_guard import mode_rule
from .content_guard import rules as content_rules
from .experiment import (
    MAX_DRAWS,
    MAX_PROMPT_CHARS,
    SIZES,
    ExpError,
    ExpSession,
    ExpTurn,
    cited_beyond,
    current_size,
    global_mentions,
    is_busy,
)
from .llm import GenerationRequest
from .models.package import utcnow
from .models.request import DEFAULT_RATING

STAGE = "face_describe"
DEFAULT_SIZE = SIZES["3:4"]  # a casting photograph
MAX_SEED = 2**31 - 1

Subject = Literal["face", "look", "set", "plan", "prop"]
# Each subject's name (as the UI says it) and test size.
SUBJECTS: dict[str, tuple[str, str]] = {
    "face": ("face", SIZES["3:4"]),
    "look": ("look", SIZES["9:16"]),
    "set": ("set", SIZES["4:3"]),
    "plan": ("set plan", SIZES["1:1"]),
    "prop": ("prop", SIZES["1:1"]),
}


def subject_for(asset_type: str, view: str) -> Subject:
    """The study subject of a design picture: its requirement's type and deliverable view."""
    if asset_type == "character_look":
        return "look"
    if asset_type == "location":
        return "plan" if view == "top-down" else "set"
    if asset_type == "prop":
        return "prop"
    return "face"


DESCRIBE_ROLE = """\
You are a casting and character-design writer. You look at a photo of a person and write one \
text-to-image prompt for MiniMax-H3-Image that recreates that person, and the photo, as \
closely as words allow. The prompt is used with no reference picture, so every visible detail \
that makes this face this face has to be in the words. Our server renders the prompt exactly \
as written (no prompt expansion). Follow the H3 rules and examples below.\
"""

DESCRIBE_GUIDE = """\
## Your input

- The photo, attached after the label `<Photo>`. It is NOT sent to the image model; your \
words are all the image model gets.
- `hint`: what the user wants you to focus on or change, if anything.
- `previous_prompt`: an earlier description of this photo, if any; improve on it.

## How to write it

Open with what the image is (e.g. "A studio casting photograph of ..." or "A candid street \
photograph of ...", matching the photo), then the framing and camera, then the person, then \
wardrobe, background and light. Keep the photo's framing, pose and viewpoint, so the render can \
be compared with it side by side. Describe the person concretely, in this order:

- apparent age range, gender presentation, build;
- skin: tone and undertone, texture, freckles, moles or marks and where they are;
- face shape, forehead, cheekbones, jaw and chin;
- eyebrows (shape, thickness, color); eyes (shape, size, spacing, lids, color, lashes);
- nose (bridge, width, tip); mouth and lips (shape, fullness, color); teeth if visible;
- ears if visible; facial hair;
- hair: color with highlights, length, texture, parting, hairline, how it is styled;
- expression, gaze direction, head tilt;
- makeup, glasses, jewelry, then clothing with fabrics, fit and colors;
- background, light (source, direction, quality), lens feel and depth of field.

Use precise visual words ("a slightly hooded upper lid", "a soft square jaw"), not judgments \
("beautiful", "striking"). Never name the person, even if you recognize them: describe traits.
Write in English. End with "The image contains no text, letters or numbers." unless the photo \
shows lettering that matters (quote it exactly). Stay under 3,500 characters.

## What may be drawn

{rules}

{mode}

In Restrictive mode, describe a type, not a replica: the general look (age range, coloring, \
face shape, hair, style), leaving out the distinctive details that identify this one person \
(unusual marks, scars, signature features), and say so in `notes`.

Return `real_person`: true when the photo shows an identifiable person (a visible face counts), \
since the prompt is meant to recreate them. If the photo itself can't be described within the \
rules (nudity, a sexualized image), return allowed=false and a one-sentence `refusal`.

## Output

Return {{"allowed": true, "real_person": true|false, "prompt": "...", "notes": "...", \
"refusal": ""}}: `prompt` is the prompt only, as plain text; `notes` is one or two short \
sentences to the user on what will be hard to match from words alone (e.g. an asymmetric \
smile) or what you left out.\
"""


class DescribeOutput(BaseModel):
    allowed: bool = True
    # Required: a description that doesn't say counts as failed (never as "no likeness").
    real_person: bool
    prompt: str = ""
    notes: str = ""
    refusal: str = ""


# Other subjects: who writes, and how. `{anchor}` says what <Picture 1> is when the tests
# are drawn from one.
_ROLES = {
    "look": "You are a costume designer. You look at a reference photo of an outfit (worn by \
anyone, or laid out) and write one prompt for MiniMax-H3-Image that dresses our character in it.",
    "set": "You are a production designer. You look at a reference photo of a place and write \
one prompt for MiniMax-H3-Image that recreates it as our story's set, empty of people.",
    "plan": "You are a production designer. You look at a reference photo (a floor plan, or the \
place itself) and write one prompt for MiniMax-H3-Image for our set's top-down plan view.",
    "prop": "You are a prop master. You look at a reference photo of an object and write one \
prompt for MiniMax-H3-Image that recreates that exact object as our story's prop.",
}
_HOW = {
    "look": """\
A full-length costume photograph, head to shoes, one person, front-facing, standing on a plain \
light grey backdrop, soft even light. Describe, in this order: the garments (cut, fabric, fit, \
colors, closures), layers, shoes, accessories and jewelry, bag; hair and makeup; condition (wet, \
torn, muddy) if the photo shows it; the pose. Never describe the photo's person: not their face, \
skin, build or age. Our character's identity comes from <Picture 1>.""",
    "set": """\
A production design photograph of the empty set, eye-level wide view. Describe the layout \
(where things are, left/right, foreground/background), the architecture, furniture and \
dressing, materials and textures, the color palette, the entrances and windows, and the light \
(source, direction, time of day). Leave out every person; describe no one.""",
    "plan": """\
An overhead plan view of the set, straight down, every wall, opening and piece of furniture in \
place, with the same inventory and coordinate frame as the set's wide view. Leave out people.""",
    "prop": """\
A product photograph of the one object, close-up, on a plain neutral background, soft light. \
Describe its shape and size (with a scale cue), materials, color and finish, wear and marks, \
and every word printed on it, quoted exactly. Leave out hands and people unless they hold it.""",
}
_ANCHOR = {
    "look": "<Picture 1> is the character's face (their current pick). Write an edit prompt: \
define the person in <Picture 1> as <Subject 1>, partially preserved (face, age, build and \
identifying marks stay), and describe the outfit from the photo, full length.",
    "set": "<Picture 1> is the set's first state (its current pick). Write an edit prompt: \
the set in <Picture 1> is <Subject 1>, partially preserved (layout, architecture, furniture and \
camera stay); describe what the photo changes (time of day, light, dressing, condition).",
    "plan": "<Picture 1> is the set's wide view (its current pick). Write an edit prompt for \
its plan view: the same inventory and coordinate frame as <Picture 1>, seen straight down.",
    "prop": "<Picture 1> is the prop's first state (its current pick). Write an edit prompt: \
the object in <Picture 1> is <Subject 1>, partially preserved (shape, materials, marks stay); \
describe the state the photo shows (worn, broken, opened).",
}
_GUIDE = """\
## Your input

- The photo, attached after the label `<Photo>`. It is NOT sent to the image model; your \
words are all the image model gets{anchor_input}.
- `hint`: what the user wants you to focus on or change, if anything.
- `previous_prompt`: an earlier description of this photo, if any; improve on it.
- `design`: what the story says this picture must show, if known. Keep its must-keep items.

## How to write it

{how}

{anchor}Use precise visual words, not judgments ("beautiful", "premium"). Write in English. \
End with "The image contains no text, letters or numbers." unless lettering matters (quote it \
exactly). Stay under 3,500 characters.

## What may be drawn

{rules}

{mode}

Return `real_person`: true only if your prompt describes an identifiable real person (it \
should not: describe no one's face). If the photo itself can't be described within the rules \
(nudity, a sexualized image), return allowed=false and a one-sentence `refusal`.

## Output

Return {{"allowed": true, "real_person": false, "prompt": "...", "notes": "...", \
"refusal": ""}}: `prompt` is the prompt only, as plain text; `notes` is one or two short \
sentences to the user on what will be hard to match from words alone, or what you left out.\
"""


def describe_instructions(
    restrictive: bool = False,
    rating: str = DEFAULT_RATING,
    subject: str = "face",
    anchored: bool = False,
) -> str:
    """The describe instructions for a subject; `anchored`: the tests are drawn from
    <Picture 1> (attached for the writer too)."""
    from .prompts import load_h3_image_examples, load_h3_image_rules

    rules, mode = content_rules(rating), mode_rule(restrictive)
    if subject == "face" or subject not in _ROLES:
        guide = DESCRIBE_GUIDE.format(rules=rules, mode=mode)
        role = DESCRIBE_ROLE
    else:
        guide = _GUIDE.format(
            how=_HOW[subject],
            anchor=f"{_ANCHOR[subject]}\n\n" if anchored else "",
            anchor_input=(", together with <Picture 1>, attached after its label"
                          if anchored else ""),
            rules=rules, mode=mode,
        )  # fmt: skip
        role = _ROLES[subject]
    parts = [role, load_h3_image_rules(), load_h3_image_examples(), guide]
    return "\n\n".join(parts)


def fixture_describe(request: GenerationRequest) -> DescribeOutput:
    """Offline stand-in: a fixed casting-photo description."""
    hint = request.context.get("hint") or ""
    opening = {
        "look": "The person in <Picture 1> is <Subject 1>. A full-length costume photograph of "
        "<Subject 1>, head to shoes, on a plain light grey backdrop.",
        "set": "A production design photograph of an empty room, eye-level wide view, soft "
        "daylight from a window on the left.",
        "plan": "An overhead plan view of an empty room, straight down.",
        "prop": "A product photograph of one object, close-up, on a plain grey background.",
    }.get(request.context.get("subject") or "face")
    if opening and not request.context.get("anchored"):
        opening = opening.replace("The person in <Picture 1> is <Subject 1>. ", "").replace(
            "<Subject 1>", "one adult person"
        )
    prompt = (
        (opening or "A studio casting photograph of one adult person, head and shoulders, "
         "front-facing, against a plain light grey backdrop, soft light from the front left.")
        + (f" {hint}." if hint else "")
        + " The image contains no text, letters or numbers."
    )  # fmt: skip
    return DescribeOutput(
        real_person=False, prompt=prompt, notes="Offline sample: a fixed description."
    )


# ---------------------------------------------------------------------------
# Planning (pure: the API saves what these return)


def new_study(title: str | None = None, subject: str = "face") -> ExpSession:
    name, size = SUBJECTS.get(subject, SUBJECTS["face"])
    return ExpSession(
        session_id=f"exp_{uuid.uuid4().hex[:12]}",
        title=title or f"New {name} study",
        kind="face",
        subject=subject,  # type: ignore[arg-type]
        size=size,
    )


def source_photo(turns: list[ExpTurn]) -> str | None:
    """The study's photo (asset id): the newest describe turn's."""
    for t in sorted(turns, key=lambda t: t.index, reverse=True):
        if t.mode == "describe" and t.attachments:
            return t.attachments[0]
    return None


def photo_likeness(turns: list[ExpTurn]) -> bool:
    """Whether any photo described in the study shows an identifiable person. Any, not just
    the newest: an earlier photo's description can be loaded back into the prompt, and the
    prompt (which never names anyone) doesn't say whose face it describes."""
    return any(t.likeness is True for t in turns if t.mode == "describe")


def _next(session: ExpSession, turns: list[ExpTurn], **fields: Any) -> ExpTurn:
    if session.kind != "face":
        raise ExpError("This is not a Face Lab study.")
    if is_busy(turns):
        raise ExpError("Wait for the current step to finish first.")
    return ExpTurn(
        turn_id=f"turn_{uuid.uuid4().hex[:12]}",
        session_id=session.session_id,
        index=max((t.index for t in turns), default=0) + 1,
        **fields,
    )


def plan_describe(
    session: ExpSession,
    turns: list[ExpTurn],
    assets: dict[str, dict[str, Any]],
    *,
    asset_id: str | None,
    hint: str,
    restrictive: bool,
    anchor: str | None = None,
    anchor_likeness: bool = False,
) -> ExpTurn:
    """A describe turn: a new photo, or the study's photo again (with a hint). `anchor`:
    the upload-style copy of the picture the tests are drawn from, shown to the writer."""
    asset_id = asset_id or source_photo(turns)
    if not asset_id:
        raise ExpError("Upload a photo to describe.")
    raw = assets.get(asset_id)
    if raw is None:
        raise ExpError(f"unknown upload {asset_id}")
    if raw.get("kind") != "image":
        raise ExpError("Face Lab describes photos, not videos.")
    return _next(
        session,
        turns,
        mode="describe",
        text=hint.strip(),
        attachments=[asset_id],
        references=[f"asset:{anchor}"] if anchor else [],
        draws=0,
        restrictive=restrictive,
        # Written around a face marked as a likeness: its tests recreate that person.
        likeness=True if anchor_likeness else None,
    )


def plan_test(
    session: ExpSession,
    turns: list[ExpTurn],
    *,
    prompt: str,
    draws: int,
    seeds: list[int] | None,
    size: str | None,
    restrictive: bool,
    provider: str | None = None,
    anchor: str | None = None,
    anchor_likeness: bool = False,
    anchor_label: str = "",
) -> ExpTurn:
    """A test render of a prompt, with the given seeds if any, drawn by H3 or another
    provider (`provider`, e.g. "local"): by itself, or, in a study whose picture is drawn
    from another, as an edit of that picture's pick (`anchor`, an upload-style copy) sent as
    <Picture 1>."""
    prompt = prompt.strip()
    if not prompt:
        raise ExpError("Write or generate a prompt first.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ExpError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters.")
    if missing := cited_beyond(prompt, 1 if anchor else 0):
        raise ExpError(
            f"The prompt cites {', '.join(missing)}, but these tests send "
            + (f"only <Picture 1> ({anchor_label or 'the picture it is drawn from'})."
               if anchor else "no pictures.")
        )  # fmt: skip
    if anchor and provider is not None:
        raise ExpError(
            f"These tests are drawn from <Picture 1> ({anchor_label or 'another picture'}); "
            "the local image server draws from text alone. Use MiniMax H3."
        )
    if gids := global_mentions(prompt):
        raise ExpError(
            f"Tests draw from the prompt alone, so they can't use @{gids[0]}. To start from "
            "that picture, describe it (Describe a picture by number)."
        )
    seeds = list(seeds or [])
    if any(not 1 <= s <= MAX_SEED for s in seeds):
        raise ExpError(f"Seeds are whole numbers from 1 to {MAX_SEED}.")
    if len(set(seeds)) != len(seeds):
        raise ExpError("Each seed can be used once per test.")
    n = len(seeds) if seeds else max(1, min(draws, MAX_DRAWS))
    if n > MAX_DRAWS:
        raise ExpError(f"At most {MAX_DRAWS} seeds per test.")
    size = current_size(size or session.size or DEFAULT_SIZE)
    if size not in SIZES.values():
        raise ExpError(f"unsupported size {size}")
    return _next(
        session,
        turns,
        mode="exact",
        text=prompt,
        prompt=prompt,
        draws=n,
        seeds=seeds,
        size=size,
        restrictive=restrictive,
        provider=provider,
        attachments=[anchor] if anchor else [],
        references=[f"asset:{anchor}"] if anchor else [],
        # Renders meant to recreate an identifiable person carry their likeness; so do
        # renders drawn from a picture marked as one.
        likeness=True if photo_likeness(turns) or anchor_likeness else None,
        finished_at=None,
        created_at=utcnow(),
    )
