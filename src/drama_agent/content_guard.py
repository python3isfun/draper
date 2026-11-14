"""Content guard for image prompts: what the image model may be asked to draw.

The images this app makes are design references and experiments, never intimate frames,
so the rules are strict and simple:

1. No nudity, sexual acts or sexually explicit imagery, ever.
2. No romantic or sexualized imagery of anyone who is, or looks, under 18.
3. No intimate, sexualized, romantic or revealing edits of an uploaded photo: an upload may
   show a real person, and sexual imagery of a real person without consent is illegal in
   many places (in the US, the TAKE IT DOWN Act). Ordinary edits of an upload are fine.

Two layers. `text_guard` is deterministic (word lists) and cheap; it catches the clear
cases in any prompt. `screen` asks the text model, which also sees the pictures, for what
word lists can't judge (who is in an upload, how old they look, what an edit implies). It
fails closed: when the check can't run, nothing is drawn.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel

from .llm import GenerationRequest

log = logging.getLogger(__name__)

STAGE = "content_guard"

_NUDITY = re.compile(
    r"\b(nude|nudes|naked|nudity|topless|bottomless|unclothed|genitals?|genitalia|"
    r"nipples?|areolae?|pubic|bare (breasts?|buttocks)|porn\w*|pornographic|erotica?|"
    r"sex (act|scene)s?|sexual (act|intercourse)s?|having sex|intercourse|orgasm\w*|"
    r"sexually explicit|x-rated|nsfw)\b",
    re.IGNORECASE,
)
_MINOR = re.compile(
    r"\b(child|children|kid|kids|toddler|baby|infant|teen|teens|teenage|teenager|"
    r"adolescent|minor|minors|underage|pre-?teen|tween|schoolgirl|schoolboy|"
    r"(little|young) (girl|boy)s?)\b",
    re.IGNORECASE,
)
_AGE = re.compile(r"\b(\d{1,2})[- ](?:years?|yrs?)[- ]old\b", re.IGNORECASE)
# Sexualized words only: a hug or a kiss on a child's forehead is not caught here (the
# model screen judges affection; this list must not block family pictures).
_SUGGESTIVE = re.compile(
    r"\b(sexy|sexual\w*|seductive\w*|sensual\w*|sultry|provocative\w*|lingerie|bikini|"
    r"underwear|bra|panties|undress\w*|stripp\w*|cleavage|making out|lovers?)\b",
    re.IGNORECASE,
)

# "Nude" as a color (fashion and makeup) and negated mentions ("no nudity") are not nudity.
_NUDE_COLOR = re.compile(
    r"\bnude(?=[- ]?(colou?r(ed)?|tone[sd]?|beige|pink)\b|\s+(lip\w*|gloss|makeup|make-up|"
    r"nails?|polish|manicure|pedicure|heels?|pumps|shoes|sandals|flats|stilettos|boots|"
    r"tights|stockings|hosiery|palette|shades?|eyeshadow|blush|leather|suede|fabric|silk|"
    r"satin|linen|dress|gown|skirt|top|blouse|sweater|cardigan|coat|trench|bag|clutch)\b)",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(no|not|without|never|non)[- ]+(\w+[- ]+)?(nudity|nude|naked)\b", re.IGNORECASE
)
# Revealing or bedroom imagery: refused for pictures that may show a real person (rule 3).
_REVEALING = re.compile(
    r"\b(lingerie|underwear|bra|bras|panties|thong|briefs|boxers|bikini|swimsuit|swimwear|"
    r"bathing suit|negligee|nightie|camisole|corset|shirtless|bare[- ]chested|undress\w*|"
    r"in bed|bed scene|between the sheets|under the covers|bedsheets?)\b",
    re.IGNORECASE,
)

NUDITY_REASON = "Images here never show nudity or sexual acts."
REAL_PERSON_REASON = (
    "This job has uploaded photos, so its images may show a real person: no underwear, "
    "swimwear or bed scenes."
)
MINOR_REASON = "No romantic or sexualized images of anyone under 18."


def text_guard(prompt: str) -> str | None:
    """Why the prompt must not be drawn (a sentence for the user), or None."""
    prompt = _NUDE_COLOR.sub("", _NEGATED.sub("", prompt))
    if _NUDITY.search(prompt):
        return NUDITY_REASON
    young = bool(_MINOR.search(prompt)) or any(int(a) < 18 for a in _AGE.findall(prompt))
    if young and _SUGGESTIVE.search(prompt):
        return MINOR_REASON
    return None


RULES = """\
1. Never: nudity, sexual acts, or sexually explicit or pornographic imagery.
2. Never: romantic, sexualized or revealing imagery of anyone who is or looks under 18. If a \
picture shows someone who looks under 18, refuse any change that makes the image romantic, \
sexual or revealing.
3. Never: an intimate, sexualized, romantic or revealing image of a real person's likeness: \
undressing, lingerie, swimwear or underwear, a kiss or embrace, a bed scene, suggestive poses \
or expressions, or a humiliating or compromising situation. A real person's likeness is an \
identifiable person from an uploaded photo, a picture marked as showing a real person's \
likeness, or anyone made to look like a named or described real person (an actress, a public \
figure, someone the user knows). Ordinary images of a real person's likeness are fine: hair, \
makeup, everyday or formal clothing, expression, pose, background, light. Anyone whose face is \
visible in an uploaded photo counts as an identifiable real person. An uploaded photo used \
only for its clothing, a place or an object passes on nobody's likeness, even if a model wears \
the clothes.
4. Everything else is allowed, including clothed romance, swimwear and a kiss or embrace \
between adult characters who don't have a real person's likeness.\
"""

# The Restrictive mode switch (top of the page; off by default). Rules 1-4 apply either way.
OPEN_MODE = """\
Open mode (the default, for exploration): a character may be made to resemble a named real \
person, and a face may be taken from an uploaded photo, for ordinary images; the result then \
has a real person's likeness and rule 3 applies to it.\
"""
RESTRICTIVE_MODE = """\
Restrictive mode: don't make anyone resemble a named or described real person, and don't take \
a face from an uploaded photo onto another figure. Turn "like <a real person>" into generic \
visible traits (hair, face shape, features) that don't add up to that person, and never write \
a real person's name in the prompt. Say so in your reply.\
"""


def mode_rule(restrictive: bool) -> str:
    return RESTRICTIVE_MODE if restrictive else OPEN_MODE


def upload_guard(prompt: str) -> str | None:
    """text_guard, plus rule 3 for images that may show a real person (a job or chat with
    uploaded photos): nothing revealing and no bed scenes."""
    if reason := text_guard(prompt):
        return reason
    if _REVEALING.search(prompt):
        return REAL_PERSON_REASON
    return None


def screen_instructions(restrictive: bool = False) -> str:
    return f"""\
You check prompts before they are sent to an image model. Decide whether the image the \
prompt asks for is allowed, by these rules:

{RULES}

{mode_rule(restrictive)}

You see the prompt, the list of pictures sent with it (each attached after its label) and \
the user's own message when there is one. Judge the image that would result, not the wording.

Return {{"allowed": true|false, "reason": "...", "real_person": true|false}}: when not \
allowed, `reason` is one short sentence to the user naming the rule, without repeating the \
prompt. `real_person`: whether the person in the resulting image would have a real person's \
likeness (see rule 3), e.g. kept from <Picture 1> when it has one.\
"""


class ScreenOutput(BaseModel):
    allowed: bool
    reason: str = ""
    # Required: a check that doesn't say counts as failed (fails closed), never as "no".
    real_person: bool


class GuardRefused(ValueError):
    """The prompt is not allowed; the message is for the user."""


async def screen(
    client: Any,
    prompt: str,
    *,
    pictures: list[dict[str, str]],
    media: list[Any] | None = None,
    message: str | None = None,
    restrictive: bool = False,
) -> ScreenOutput:
    """Raise GuardRefused unless the prompt may be drawn; returns the verdict (with
    `real_person`). `pictures`: one entry per <Picture N>, {"picture": "<Picture 1>",
    "kind": ...}; `media`: the pictures themselves (MediaPart), so the model can see who is
    in them. Fails closed: if the check can't run, the prompt is refused."""
    if reason := text_guard(prompt) or (text_guard(message) if message else None):
        raise GuardRefused(reason)
    request = GenerationRequest(
        stage=STAGE,  # type: ignore[arg-type]
        instructions=screen_instructions(restrictive),
        context={"prompt": prompt, "pictures": pictures, "user_message": message},
        media=list(media or []),
    )
    try:
        out = await client.generate(request, ScreenOutput)
    except Exception as exc:
        log.warning("content screen failed", exc_info=True)
        raise GuardRefused(
            f"Couldn't run the content check ({type(exc).__name__}), so nothing was drawn. "
            "Try again."
        ) from exc
    if not out.allowed:
        raise GuardRefused(out.reason.strip() or "This image isn't allowed here.")
    return out
