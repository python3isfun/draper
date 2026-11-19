"""Content guard for image prompts: what the image model may be asked to draw.

What an image may show follows its story's rating, as the MPA rates American films
(models/request.py; a picture outside any story is PG-13):

1. Never, at any rating: sex acts shown explicitly, genitals, or pornographic imagery.
   G and PG: no nudity and nothing suggestive (no lingerie, undressing or bed scenes).
   PG-13: suggestive is fine (lingerie, a covered bed scene, implied intimacy), no nudity.
   R: nudity of adult characters as in an R-rated film (bare breasts or buttocks, a love
   scene under sheets), never explicit.
2. No romantic or sexualized imagery of anyone who is, or looks, under 18.
3. No sexualized images of a real person's likeness (an identifiable person from an
   uploaded photo, or someone made to resemble a named real person): sexual imagery of a
   real person without consent is illegal in many places (in the US, the TAKE IT DOWN
   Act). Ordinary images and clothed romance are fine in the default (open) mode; the
   Restrictive mode switch also rules out likeness by name and romance with a likeness.

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
from .models.request import DEFAULT_RATING, as_rating

log = logging.getLogger(__name__)

STAGE = "content_guard"

# Never drawn, at any rating.
_EXPLICIT = re.compile(
    r"\b(genitals?|genitalia|pubic|porn\w*|pornographic|erotica?|sex acts?|"
    r"sexual (act|intercourse)s?|having sex|intercourse|orgasm\w*|sexually explicit|"
    r"x-rated|nsfw)\b",
    re.IGNORECASE,
)
# Nudity: R only.
_NUDITY = re.compile(
    r"\b(nude|nudes|naked|nudity|topless|bottomless|unclothed|nipples?|areolae?|"
    r"bare (breasts?|buttocks)|sex scenes?)\b",
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
# A bare shoulder, arm or foot ("naked shoulder exposed", "naked feet") is not nudity either.
# Only parts that show nothing intimate (not the back or chest); the model screen still
# judges the whole picture.
# The whole phrase is set aside, and only when the word bares that one part: in "naked arms
# and chest" it may bare more, so it still counts.
_BARE_PART = re.compile(
    r"\b(naked|nude)[- ]+(left |right )?(shoulders?|arms?|forearms?|elbows?|wrists?|"
    r"hands?|fingers?|feet|foot|toes|ankles?|legs?|calf|calves|knees?|shins?|neck|"
    r"collarbones?|throat|face)\b(?!\s*(,|&|/|\+|and\b|or\b|with\b|plus\b))",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(no|not|without|never|non)[- ]+(\w+[- ]+)?(nudity|nude|naked)\b", re.IGNORECASE
)
# Sexualized imagery (underwear, undressing, bed scenes): refused for pictures that may show
# a real person (rule 3). Swimwear is separate: refused only under the restrictive rules.
_REVEALING = re.compile(
    r"\b(lingerie|underwear|bra|bras|panties|thong|briefs|boxers|negligee|nightie|corset|"
    r"undress\w*|in bed|bed scene|between the sheets|under the covers|bedsheets?)\b",
    re.IGNORECASE,
)

# Suggestive imagery of adults: PG-13 and up. A narrower list than _SUGGESTIVE (which only
# counts next to a minor): "lovers" or "in bed" alone are fine at PG; the model screen
# judges the picture.
_ADULT_SUGGESTIVE = re.compile(
    r"\b(sexy|seductive\w*|sultry|provocative\w*|lingerie|underwear|bra|bras|panties|thong|"
    r"negligee|corset|undress\w*|stripp\w*|cleavage|making out|bed scene|"
    r"between the sheets)\b",
    re.IGNORECASE,
)

_SWIMWEAR = re.compile(
    r"\b(bikini|swimsuit|swimwear|bathing suit|swim trunks|shirtless|bare[- ]chested)\b",
    re.IGNORECASE,
)

EXPLICIT_REASON = "Images here never show sex acts, genitals or pornography, at any rating."
REAL_PERSON_REASON = (
    "This job has uploaded photos, so its images may show a real person: no nudity, "
    "underwear, lingerie, swimwear or bed scenes, at any rating."
)
MINOR_REASON = "No romantic or sexualized images of anyone under 18."


def _clean(prompt: str) -> str:
    """The prompt without what only looks like nudity: negations ("no nudity"), "nude" as a
    color and a bare shoulder, arm or foot."""
    return _BARE_PART.sub("", _NUDE_COLOR.sub("", _NEGATED.sub("", prompt)))


def nudity_reason(rating: str) -> str:
    return f"Images here are rated {rating}: no nudity (that takes a story rated R)."


def suggestive_reason(rating: str) -> str:
    return (
        f"Images here are rated {rating}: nothing suggestive, such as lingerie, underwear, "
        "undressing or a bed scene (that takes a story rated PG-13 or R)."
    )


NUDITY_REASON = nudity_reason(DEFAULT_RATING)  # a story rated PG-13, or no story


def text_guard(prompt: str, rating: str = DEFAULT_RATING) -> str | None:
    """Why the prompt must not be drawn at the story's rating (a sentence for the user), or
    None."""
    rating = as_rating(rating)
    prompt = _clean(prompt)
    if _EXPLICIT.search(prompt):
        return EXPLICIT_REASON
    young = bool(_MINOR.search(prompt)) or any(int(a) < 18 for a in _AGE.findall(prompt))
    if young and (_SUGGESTIVE.search(prompt) or _NUDITY.search(prompt)):
        return MINOR_REASON
    if rating != "R" and _NUDITY.search(prompt):
        return nudity_reason(rating)
    if rating in ("G", "PG") and _ADULT_SUGGESTIVE.search(prompt):
        return suggestive_reason(rating)
    return None


_NOTHING_SUGGESTIVE = "1. Never: nudity, sexual acts, or sexually explicit or pornographic \
imagery; and, as the story is rated {rating}, nothing suggestive: no lingerie or underwear, \
undressing, bed scenes, or sexualized poses or expressions."
_RULE_1 = {
    "G": _NOTHING_SUGGESTIVE.format(rating="G"),
    "PG": _NOTHING_SUGGESTIVE.format(rating="PG"),
    "PG-13": "1. Never: nudity, sexual acts, or sexually explicit or pornographic imagery. As \
the story is rated PG-13, suggestive images of adults are allowed without nudity: lingerie, \
a bed scene with the couple covered, intimacy that is implied.",
    "R": "1. Never: a sex act shown explicitly, genitals, or pornographic imagery. As the story \
is rated R, nudity of adult characters is allowed as in an R-rated film: bare breasts or \
buttocks, a love scene under sheets, simulated intimacy that shows no sex act explicitly.",
}


def rules(rating: str = DEFAULT_RATING) -> str:
    """The content rules at a story's rating, for the models that write and check prompts."""
    rating = as_rating(rating)
    return (
        f"The story is rated {rating} (MPA film ratings: G, PG, PG-13, R).\n"
        + _RULE_1[rating]
        + "\n"
        + _RULES_2_TO_4
    )


_RULES_2_TO_4 = """\
2. Never: romantic, sexualized or revealing imagery of anyone who is or looks under 18. If a \
picture shows someone who looks under 18, refuse any change that makes the image romantic, \
sexual or revealing.
3. Never, at any rating: a sexualized image of a real person's likeness: nudity, lingerie or \
underwear, undressing, \
a bed scene or bedroom intimacy, suggestive or sexualized poses or expressions, or a \
degrading or humiliating situation. A real person's likeness is an identifiable person from \
an uploaded photo (anyone whose face is visible in one counts), a picture marked as showing a \
real person's likeness, or anyone made to look like a named or described real person (an \
actress, a public figure, someone the user knows). An uploaded photo used only for its \
clothing, a place or an object passes on nobody's likeness, even if a model wears the clothes.
4. Everything else is allowed, including clothed romance (a kiss, an embrace, holding hands) \
and swimwear between adult characters.\
"""
RULES = rules()  # at the default rating (PG-13)

# The Restrictive mode switch (top of the page; off by default). Rules 1-4 apply either way.
OPEN_MODE = """\
Open mode (the default, for exploration): a character may be made to resemble a named real \
person, and a face may be taken from an uploaded photo. That likeness may appear in ordinary \
and clothed romantic scenes (a kiss, an embrace, swimwear at a beach); production and legal \
clear any likeness before it is used. Rule 3 still applies to it.\
"""
RESTRICTIVE_MODE = """\
Restrictive mode: don't make anyone resemble a named or described real person, and don't take \
a face from an uploaded photo onto another figure. Turn "like <a real person>" into generic \
visible traits (hair, face shape, features) that don't add up to that person, and never write \
a real person's name in the prompt. Say so in your reply. A real person's likeness that is \
already there (an uploaded photo, a picture marked as having one) gets no romance or revealing \
clothing either: no kiss or embrace, no swimwear.\
"""


def mode_rule(restrictive: bool) -> str:
    return RESTRICTIVE_MODE if restrictive else OPEN_MODE


def upload_guard(
    prompt: str, *, restrictive: bool = True, rating: str = DEFAULT_RATING
) -> str | None:
    """text_guard at the story's rating, plus rule 3 for images that may show a real person
    (a job with uploaded character photos): nothing sexualized or nude at any rating, and
    under the restrictive rules (the design board's, always) no swimwear either."""
    if reason := text_guard(prompt, rating):
        return reason
    clean = _clean(prompt)
    if (
        _REVEALING.search(prompt)
        or _NUDITY.search(clean)
        or (restrictive and _SWIMWEAR.search(prompt))
    ):
        return REAL_PERSON_REASON
    return None


def screen_instructions(restrictive: bool = False, rating: str = DEFAULT_RATING) -> str:
    return f"""\
You check prompts before they are sent to an image model. Decide whether the image the \
prompt asks for is allowed, by these rules:

{rules(rating)}

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
    rating: str = DEFAULT_RATING,
) -> ScreenOutput:
    """Raise GuardRefused unless the prompt may be drawn; returns the verdict (with
    `real_person`). `pictures`: one entry per <Picture N>, {"picture": "<Picture 1>",
    "kind": ...}; `media`: the pictures themselves (MediaPart), so the model can see who is
    in them. `rating`: the story's (models/request.py). Fails closed: if the check can't run,
    the prompt is refused."""
    if reason := text_guard(prompt, rating) or (text_guard(message, rating) if message else None):
        raise GuardRefused(reason)
    request = GenerationRequest(
        stage=STAGE,  # type: ignore[arg-type]
        instructions=screen_instructions(restrictive, rating),
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
