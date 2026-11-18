"""The keyframe judge: its guide and output."""

from __future__ import annotations

from pydantic import BaseModel

from ..llm import GenerationRequest
from .models import CheckItem

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
- **set**: the place matches the set picture (layout, materials, palette), and what is \
behind each person matches `scene.set.layout` for this camera direction (e.g. a window \
behind a headboard that stands against a solid wall is a major fail).
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
