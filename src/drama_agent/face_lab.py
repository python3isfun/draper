"""Face Lab: turn an uploaded photo into the most descriptive text prompt, then test that
prompt by itself (text to image) with chosen draws and seeds.

A Face Lab study is an Experiment session of kind "face", so it shares storage, image
numbers, the content guard, likeness tracking, retries and the GPU queue:

- a **describe** turn (mode "describe") sends the photo to the text model, which writes a
  text-to-image prompt recreating the person as closely as text allows. Nothing is drawn.
- a **test** turn (mode "exact", no pictures) renders a prompt from text alone, with fixed
  seeds when given, so two wordings can be compared seed for seed.

A test inherits the photo's likeness: if the photo shows an identifiable person, the
renders are meant to look like them, so content rule 3 applies to them.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from .content_guard import RULES as CONTENT_RULES
from .content_guard import mode_rule
from .experiment import (
    MAX_DRAWS,
    MAX_PROMPT_CHARS,
    SIZES,
    ExpError,
    ExpSession,
    ExpTurn,
    cited_beyond,
    is_busy,
)
from .llm import GenerationRequest
from .models.package import utcnow

STAGE = "face_describe"
DEFAULT_SIZE = SIZES["3:4"]  # a casting photograph
MAX_SEED = 2**31 - 1

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


def describe_instructions(restrictive: bool = False) -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    guide = DESCRIBE_GUIDE.format(rules=CONTENT_RULES, mode=mode_rule(restrictive))
    parts = [DESCRIBE_ROLE, load_h3_image_rules(), load_h3_image_examples(), guide]
    return "\n\n".join(parts)


def fixture_describe(request: GenerationRequest) -> DescribeOutput:
    """Offline stand-in: a fixed casting-photo description."""
    hint = request.context.get("hint") or ""
    prompt = (
        "A studio casting photograph of one adult person, head and shoulders, front-facing, "
        "against a plain light grey backdrop, soft light from the front left."
        + (f" {hint}." if hint else "")
        + " The image contains no text, letters or numbers."
    )
    return DescribeOutput(
        real_person=False, prompt=prompt, notes="Offline sample: a fixed description."
    )


# ---------------------------------------------------------------------------
# Planning (pure: the API saves what these return)


def new_study(title: str | None = None) -> ExpSession:
    return ExpSession(
        session_id=f"exp_{uuid.uuid4().hex[:12]}",
        title=title or "New face study",
        kind="face",
        size=DEFAULT_SIZE,
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
) -> ExpTurn:
    """A describe turn: a new photo, or the study's photo again (with a hint)."""
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
        draws=0,
        restrictive=restrictive,
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
) -> ExpTurn:
    """A test render of a prompt by itself (no pictures), with the given seeds if any."""
    prompt = prompt.strip()
    if not prompt:
        raise ExpError("Write or generate a prompt first.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ExpError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters.")
    if missing := cited_beyond(prompt, 0):
        raise ExpError(
            f"The prompt cites {', '.join(missing)}, but Face Lab tests send no pictures."
        )
    seeds = list(seeds or [])
    if any(not 1 <= s <= MAX_SEED for s in seeds):
        raise ExpError(f"Seeds are whole numbers from 1 to {MAX_SEED}.")
    if len(set(seeds)) != len(seeds):
        raise ExpError("Each seed can be used once per test.")
    n = len(seeds) if seeds else max(1, min(draws, MAX_DRAWS))
    if n > MAX_DRAWS:
        raise ExpError(f"At most {MAX_DRAWS} seeds per test.")
    size = size or session.size or DEFAULT_SIZE
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
        # Renders meant to recreate an identifiable person carry their likeness.
        likeness=True if photo_likeness(turns) else None,
        finished_at=None,
        created_at=utcnow(),
    )
