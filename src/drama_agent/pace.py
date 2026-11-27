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
    setup_s: tuple[float, float]  # how long one view lasts where people talk
    moving: str  # the share of shots that move the camera, in words ("at least half")
    words: tuple[int, int]  # the words of physical action per shot
    feel: str


RULES: dict[str, PaceRule] = {
    "brisk": PaceRule("Brisk", 1.5, (1.5, 3), "at least three in four", (45, 70),
                      "Every clip moves: someone acts, the camera travels, and a new setup "
                      "comes every two or three seconds in dialogue."),
    "measured": PaceRule("Measured", 2.5, (2.5, 5), "at least half", (40, 60),
                         "Clips move steadily; a beat may breathe for a moment before the "
                         "next action."),
    "slow_burn": PaceRule("Slow burn", 4.0, (4, 8), "at least one in three", (35, 55),
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


# The shortest storyboard shot: the video model renders no clip under 4 s, so a shorter
# shot is padded to it and holds on a face after its line (hard_checks.check_coverage).
MIN_SHOT_MS = 4000

# How a scene is covered, for a vertical frame. Shot sizes the coverage check reads from
# `framing` (hard_checks.check_coverage): the first words of the framing name the size.
COVERAGE_RULES = (
    "- **A shot is one clip of 4 to 8 s, two or three beats, up to three lines.** The video "
    "model renders nothing shorter than 4 s, and the cut planner cuts each clip into the "
    "views it needs (the speaker, the listener, the hands), so the fast cutting happens "
    "inside your shots, not between them. Never write a shot under 4 s: an insert or a "
    "reaction is a moment of the shot it belongs to. A shot over 8 s needs a camera move "
    "that carries it (a track with the walk, a slow push through the turn) and says so.\n"
    "- **A line starts and ends inside one shot.** A shot ends on the last word of its last "
    "line or on the action that finishes it; a line never runs over the cut into the next "
    "shot, and no shot holds only the first or last word of someone's line.\n"
    "- **Cover a scene with a pattern, not a list of two-shots**: one wider shot to place "
    "the people, then singles and over-the-shoulder shots that alternate with the lines, a "
    "close-up for the turn of the scene, an insert for the prop or the hands that matter. "
    "The frame is vertical: medium close-up is the default size, and a medium-wide two-shot "
    "leaves both faces small, so use it to establish or to show a move across the room, "
    "not to carry an exchange.\n"
    '- **Begin each `framing` with its size and subject**, e.g. "Medium close-up single of '
    'Lila", "Over-the-shoulder on Derek, Lila\'s shoulder frame left", "Wide two-shot", '
    '"Insert of the card in her hand". Two consecutive shots never share the same size '
    "and the same people: change the size, the angle or who the shot is about. In a scene, "
    "at most one shot in two is a two-shot or group shot.\n"
    "- **Cut on movement or on a line**: a shot ends on the action or the word that the next "
    "shot picks up, not on a held look."
)


def storyboard_rules(pace: object) -> str:
    """The storyboard step's pacing rules at this pace."""
    r = rule(pace)
    lo, hi = r.words
    return (
        f"Pace: {r.label.lower()}. {r.feel}\n"
        f"{COVERAGE_RULES}\n"
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
        f"- **A new view every {r.setup_s[0]:g} to {r.setup_s[1]:g} s where people talk**: a "
        "clip that holds two lines or more is cut at the change of speaker, or onto the "
        "listener for the line that lands on them; only a clip with one line and one "
        "action holds a single view.\n"
        "- **Each sub-shot's `action` is visible physical movement**: what hands do, what is "
        "picked up, used or put down, where people go, for the speaker too while they talk. "
        "A smile, a look or a raised eyebrow alone is not action; take the business from the "
        "shot's action, its props and the set."
    )


def story_pace(repo: Any, package_id: str | None) -> Pace:
    """A story's pace: the one set on its Overview, else its newest job's."""
    return story_setting(repo, package_id, "pace", lambda o: as_pace(o.pace), as_pace,
                         DEFAULT_PACE)  # fmt: skip
