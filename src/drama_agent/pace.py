"""A story's pace: how its storyboard and cut plans keep the clips moving.

Every pace holds the same rules (the long setup goes to whoever acts or speaks, a listener
has something to do, the camera moves unless there is a reason not to, each shot's action is
physical business); the pace sets their numbers. It is chosen with the story (JobOptions) and
can be changed later on its Overview, like the rating; it applies from the next storyboard
revision and the next cut plan on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .story_settings import story_setting

Pace = Literal["brisk", "measured", "slow_burn"]
PACES: tuple[Pace, ...] = ("brisk", "measured", "slow_burn")
DEFAULT_PACE: Pace = "brisk"


@dataclass(frozen=True)
class PaceRule:
    label: str
    reaction_s: float  # the longest a cut holds on a listener or a reaction
    moving: str  # the share of shots that move the camera, in words ("at least half")
    words: tuple[int, int]  # the words of physical action per shot
    feel: str


RULES: dict[str, PaceRule] = {
    "brisk": PaceRule("Brisk", 1.5, "at least three in four", (45, 70),
                      "Every clip moves: someone acts, the camera travels, and a new setup "
                      "comes every two or three seconds in dialogue."),
    "measured": PaceRule("Measured", 2.5, "at least half", (40, 60),
                         "Clips move steadily; a beat may breathe for a moment before the "
                         "next action."),
    "slow_burn": PaceRule("Slow burn", 4.0, "at least one in three", (35, 55),
                          "Tension builds in held moments, but even a held moment has "
                          "movement in it: a hand, a breath, a slow push in."),
}  # fmt: skip


def pace_name(value: str) -> str:
    """A pace's name as stored, from any case or spelling ("Slow burn", "slow-burn")."""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def as_pace(value: object) -> Pace:
    """A pace from its name in any case or spelling; else the default."""
    name = pace_name(value) if isinstance(value, str) else ""
    return name if name in PACES else DEFAULT_PACE  # type: ignore[return-value]


def rule(pace: object) -> PaceRule:
    return RULES[as_pace(pace)]


def storyboard_rules(pace: object) -> str:
    """The storyboard step's pacing rules at this pace."""
    r = rule(pace)
    lo, hi = r.words
    return (
        f"Pace: {r.label.lower()}. {r.feel}\n"
        "- Each shot's visual_action is physical business, not a reaction: what hands do, "
        f"what is picked up, used, put down, where people move. Write {lo} to {hi} words of "
        'it per shot. "Tries not to smile" or "dismisses the objection" alone is not '
        "action; say what the body does while it happens.\n"
        "- Give the screen time to whoever acts or speaks. A listener on screen is doing "
        "something (pouring, packing, fixing a cuff, crossing to the window), never only "
        "watching.\n"
        f"- Move the camera in {r.moving} of the shots: a slow push in, a truck or pan with "
        'the action, a small reframe, handheld for urgency. Write "locked off" (or '
        '"static") only with the reason in the same field, e.g. "Locked off: the reveal plays '
        'in the doorway".'
    )


def planner_rules(pace: object) -> str:
    """The cut planner's pacing rules at this pace."""
    r = rule(pace)
    return (
        f"## Pace: {r.label.lower()}\n\n"
        f"{r.feel}\n\n"
        "- **The long setup goes to whoever acts or speaks.** Cut to a listener or a reaction "
        f"for at most {r.reaction_s:g} s, then back to the action; never spend the rest of a "
        "clip on someone watching what happens off frame.\n"
        "- **A listener has something to do**: give them the business the shot's action "
        "and props allow (a hand on the cup, folding the letter, turning away), not a held "
        "expression.\n"
        f"- **Move the camera in {r.moving} of the sub-shots**: a slow push in, a truck or "
        "pan with the action, a small reframe. Write `still` only for a reason (an insert, a "
        'reveal held in frame) and say it. A storyboard camera of "locked off" or '
        '"static" that gives no reason is not one: move gently instead.\n'
        "- **Each sub-shot's `action` is visible physical movement**: what hands do, what is "
        "picked up, used or put down, where people go, for the speaker too while they talk. "
        "A smile, a look or a raised eyebrow alone is not action; take the business from the "
        "shot's action, its props and the set."
    )


def reaction_holds(
    subs: list[Any], lines: list[dict[str, Any]], shot_people: list[str], pace: object
) -> list[tuple[int, int, list[str]]]:
    """The sub-shots that hold a listener too long at this pace: (index, ms, off-screen
    speakers), for a sub-shot longer than the pace allows in which only people off screen
    of the shot speak (the camera stays on someone listening). A silent sub-shot is not
    judged (it may be someone acting alone, which only the words can tell), nor a voice
    from outside the shot (a phone, a narrator). `lines` are the shot's lines
    (speaker_id, start_ms), timed in the clip."""
    limit = round(rule(pace).reaction_s * 1000)
    out = []
    for sub in subs:
        length = sub.end_ms - sub.start_ms
        said = [x for x in lines if sub.start_ms <= x["start_ms"] < sub.end_ms
                and x["speaker_id"] in shot_people]  # fmt: skip
        if length <= limit or not sub.people or not said:
            continue
        if all(x["speaker_id"] not in sub.people for x in said):
            away = list(dict.fromkeys(x["speaker_id"] for x in said))
            out.append((sub.index, length, away))
    return out


def story_pace(repo: Any, package_id: str | None) -> Pace:
    """A story's pace: the one set on its Overview, else its newest job's."""
    return story_setting(repo, package_id, "pace", lambda o: as_pace(o.pace), as_pace,
                         DEFAULT_PACE)  # fmt: skip
