"""The sub-shot planner: its guide, context and output, turned into sub-shots."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..llm import GenerationRequest
from ..models import ProductionPackage
from ..models.storyboard import Shot
from ..pace import DEFAULT_PACE, planner_rules
from .common import _names
from .models import COVERAGE_MS, MAX_SUB_SHOTS, MIN_SUB_SHOT_MS, SubShot
from .plans import clip_ms, make_sub_shots, shot_lines
from .writer import shot_context

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
one of `shot.people` is a voice from outside the shot. `scene`: where and when, and \
`scene.set.layout`, where things are in the room: write each framing's background from it \
(what wall, window or door is behind whom). `problems`: \
what was wrong with your last plan, if any; fix every one.

Your plan is the spec video production renders from: its prompt follows your cuts, \
timestamps, framing and screen positions exactly, and each cut gets a keyframe drawn from \
your description. Make it clean and complete.

## How to cut

- **Cut for new information**: a new subject, space, state, viewpoint or time. A small \
reframe or a push-in is a camera move inside one sub-shot, not a cut.
- **Dialogue between people is covered speaker by speaker**: cut to the speaker (a single \
or an over-the-shoulder) shortly before their line starts, and stay on them until it ends; \
a short reaction can follow in the gap between lines (see Pace for how short). Keep a two-shot for a line only when the \
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


def plan_instructions(pace: object = DEFAULT_PACE, cuts: int | None = None) -> str:
    """The planner's instructions at the shot's pace (pace.py), and with `cuts`, the number
    of sub-shots the director asked for."""
    guide = PLAN_GUIDE.format(
        max_sub=MAX_SUB_SHOTS, min_s=MIN_SUB_SHOT_MS / 1000, coverage_s=COVERAGE_MS / 1000
    )
    out = f"{PLAN_ROLE}\n\n{guide}\n\n{planner_rules(pace)}"
    if cuts is not None:
        out += f"\n\n## Cuts\n\n{cuts_rule(cuts)}"
    return out


def cuts_rule(cuts: int) -> str:
    """The director's cut count, as the planner is told it."""
    if cuts == 1:
        return ("The director asked for **exactly 1 sub-shot**: the whole clip is one view, "
                "with the camera moving inside it; this overrides the coverage rule.")  # fmt: skip
    return (f"The director asked for **exactly {cuts} sub-shots**. Put each cut where the "
            "action or the speaker changes (a new subject, a reaction, the next line), never "
            "in the middle of a line, and make each cut a new view; this overrides \"a held "
            "moment with one view is one sub-shot\".")  # fmt: skip


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
