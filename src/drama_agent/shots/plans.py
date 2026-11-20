"""Sub-shot plans: the cuts of a shot, their lines, the checks a plan must pass, and a plan
as stored or the default single cut."""

from __future__ import annotations

import itertools
import re
from typing import Any

from ..models import ProductionPackage
from ..models.storyboard import Shot
from ..pace import reaction_holds
from ..pace import rule as pace_rule
from .common import _dialogue, _names
from .models import (
    COVERAGE_MS,
    MAX_CLIP_S,
    MAX_SUB_SHOTS,
    MIN_CLIP_S,
    MIN_SUB_SHOT_MS,
    ShotPlan,
    ShotTake,
    SubShot,
)


def clip_ms(shot: Shot) -> int:
    """The shot's clip length as H3 renders it: whole seconds from 4 to 15, rounded up so
    every line the storyboard times inside the shot is inside the clip."""
    return max(MIN_CLIP_S, min(MAX_CLIP_S, -(-shot.duration_ms // 1000))) * 1000


def single_plan(pkg: ProductionPackage, shot: Shot) -> ShotPlan:
    """The plan of a shot nobody has planned: the whole clip is one sub-shot."""
    return ShotPlan(
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        shot_id=shot.shot_id,
        sub_shots=[
            SubShot(index=1, start_ms=0, end_ms=clip_ms(shot), framing=shot.framing,
                    camera=shot.camera, action=shot.visual_action,
                    people=list(shot.character_ids))
        ],
    )  # fmt: skip


def shot_lines(pkg: ProductionPackage, shot: Shot) -> list[dict[str, Any]]:
    """The shot's lines of dialogue, timed from the start of its clip (the one source of
    line timing for the planner, the writer, the judge, the checks and the export)."""
    names = _names(pkg)
    lines = _dialogue(pkg)
    out = []
    for c in shot.dialogue_cues:
        line = lines.get(c.dialogue_id)
        if line is None:
            continue
        out.append({
            "dialogue_id": c.dialogue_id,
            "speaker_id": line.character_id,
            "speaker": names.get(line.character_id, line.character_id),
            "text": line.text,
            "start_ms": max(0, c.start_ms - shot.start_ms),
            "end_ms": max(0, c.end_ms - shot.start_ms),
        })  # fmt: skip
    return sorted(out, key=lambda x: x["start_ms"])


def sub_shot_lines(lines: list[dict[str, Any]], sub: SubShot) -> list[dict[str, Any]]:
    """The lines spoken in a sub-shot (by their start), each saying whether its speaker is
    on screen, off screen (and on which side) or a voice from outside the shot."""
    out = []
    for x in lines:
        if not sub.start_ms <= x["start_ms"] < sub.end_ms:
            continue
        who = x["speaker_id"]
        where = ("on screen" if who in sub.people
                 else f"off screen {sub.off_screen[who]}" if who in sub.off_screen
                 else "voice")  # fmt: skip
        out.append({**x, "where": where})
    return out


class PlanProblems(ValueError):
    """A plan that isn't a clean spec; `problems` lists why, one sentence each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(" | ".join(problems))
        self.problems = problems


TOO_LONG = "the storyboard shot runs"  # the start of the problem a plan can't fix

ONE_CUT = "dialogue held in one cut"  # the start of the coverage problem

LISTENER = "a listener held"  # the start of the pace problem (check_plan with a pace)

# A storyboard that says to hold the two-shot (in its framing, camera or notes).
_HOLD_TWO_SHOT = re.compile(r"\bhold(?:s|ing)?\b[^.;]{0,30}\btwo[- ]shot\b", re.IGNORECASE)


def advisory(problem: str) -> bool:
    """A problem flagged on the board and in the export but never refused on a save: a shot
    too long for H3 (no plan fixes it), dialogue held in one cut (a person may hold the
    two-shot on purpose) and a listener held longer than the pace allows (a judgement)."""
    return problem.startswith((TOO_LONG, ONE_CUT, LISTENER))


def holds_two_shot(shot: Shot) -> bool:
    """Whether the storyboard asks to keep the shot on one two-shot ("hold the two-shot")."""
    text = f"{shot.framing} {shot.camera} {shot.generation_notes} {shot.continuity}"
    return bool(_HOLD_TWO_SHOT.search(text))


def coverage_cut(lines: list[dict[str, Any]], clip: int) -> int | None:
    """Where a clip whose lines come from two or more people could cut to the next speaker:
    the first gap between two different speakers' lines with a moment at least
    MIN_SUB_SHOT_MS from both ends of the clip (the middle of the usable gap). None when the
    lines come from one person, or no such moment exists."""
    ordered = sorted(lines, key=lambda x: x["start_ms"])
    for a, b in itertools.pairwise(ordered):
        if a["speaker_id"] == b["speaker_id"]:
            continue
        lo = max(a["end_ms"], MIN_SUB_SHOT_MS)
        hi = min(b["start_ms"], clip - MIN_SUB_SHOT_MS)
        if lo <= hi:
            return (lo + hi) // 2
    return None


def check_plan(
    pkg: ProductionPackage, shot: Shot, subs: list[SubShot], pace: str | None = None
) -> list[str]:
    """Where a sub-shot plan isn't a clean spec for video production (empty if it is):
    the clip's length, every line wholly inside the clip and inside one sub-shot (no cut in
    the middle of a line), each speaker on screen or placed off screen, every on-screen
    person (and only they) placed in the frame, each cut changing the framing, screen sides
    that hold across cuts (the 180-degree rule), and dialogue between two people covered
    speaker by speaker when the clip is long enough and a cut can fall between their lines
    (unless the storyboard holds the two-shot). With a `pace` (pace.py), also a listener held
    longer than it allows while someone off screen speaks: the planner is asked to fix it once.
    See `advisory` for the problems that are flagged but never refused."""
    names = _names(pkg)
    out: list[str] = []
    clip = subs[-1].end_ms
    if shot.duration_ms > MAX_CLIP_S * 1000:
        out.append(f"{TOO_LONG} {shot.duration_ms / 1000:g} s, longer than H3's "
                   f"{MAX_CLIP_S} s clips: split it in the storyboard")  # fmt: skip
    if clip % 1000 or not MIN_CLIP_S * 1000 <= clip <= MAX_CLIP_S * 1000:
        out.append(f"the clip runs {clip / 1000:g} s, but H3 renders whole seconds from "
                   f"{MIN_CLIP_S} to {MAX_CLIP_S}")  # fmt: skip
    cuts = [s.start_ms for s in subs[1:]]
    in_shot = set(shot.character_ids)
    spoken: list[dict[str, Any]] = []  # the lines that start inside the clip
    for x in shot_lines(pkg, shot):
        said = f'{x["speaker"]}\'s line "{x["text"][:40]}"'
        if x["start_ms"] >= clip:
            out.append(f"{said} starts at {x['start_ms'] / 1000:g} s, after the clip ends")
            continue
        spoken.append(x)
        if x["end_ms"] > clip:
            out.append(f"{said} runs to {x['end_ms'] / 1000:g} s, past the clip's end at "
                       f"{clip / 1000:g} s")  # fmt: skip
        if mid := [c for c in cuts if x["start_ms"] < c < x["end_ms"]]:
            out.append(f"the cut at {mid[0] / 1000:g} s falls in the middle of {said} "
                       f"({x['start_ms'] / 1000:g}-{x['end_ms'] / 1000:g} s)")  # fmt: skip
        sub = next(s for s in subs if s.start_ms <= x["start_ms"] < s.end_ms)
        who = x["speaker_id"]
        if who in in_shot and who not in sub.people and who not in sub.off_screen:
            out.append(f"{x['speaker']} speaks in sub-shot {sub.index} but is neither on "
                       "screen nor placed off screen")  # fmt: skip
    on_screen_lines = [x for x in spoken if x["speaker_id"] in in_shot]
    if (
        len(subs) == 1
        and clip >= COVERAGE_MS
        and not holds_two_shot(shot)
        and (cut := coverage_cut(on_screen_lines, clip))
    ):
        out.append(f"{ONE_CUT}: two people speak in this {clip / 1000:g} s clip, so cover "
                       f"them speaker by speaker (2 or 3 sub-shots, e.g. a cut at "
                       f"{cut / 1000:g} s), or note \"hold the two-shot\" in the storyboard")  # fmt: skip
    for sub in subs:
        for who in sub.people:
            if who not in sub.placement:
                out.append(f"sub-shot {sub.index}: where is {names.get(who, who)} in the frame "
                           "(left, center or right)?")  # fmt: skip
        if extra := [w for w in sub.placement if w in in_shot and w not in sub.people]:
            out.append(f"sub-shot {sub.index} gives a frame position to "
                       f"{', '.join(names.get(w, w) for w in extra)}, who isn't on screen in it")  # fmt: skip
        if stray := [w for w in [*sub.placement, *sub.off_screen] if w not in in_shot]:
            out.append(f"sub-shot {sub.index} places {', '.join(stray)}, who isn't in the shot")
        if both := [w for w in sub.off_screen if w in sub.people]:
            out.append(f"sub-shot {sub.index} has {', '.join(names.get(w, w) for w in both)} "
                       "both on and off screen")  # fmt: skip
    for a, b in itertools.pairwise(subs):
        if " ".join(a.framing.lower().split()) == " ".join(b.framing.lower().split()):
            out.append(f"sub-shots {a.index} and {b.index} have the same framing; a cut must "
                       "change the view")  # fmt: skip
        for who in shot.character_ids:
            was = a.placement.get(who) if who in a.people else a.off_screen.get(who)
            now = b.placement.get(who) if who in b.people else b.off_screen.get(who)
            if {was, now} == {"left", "right"}:
                out.append(f"{names.get(who, who)} moves from frame {was} to frame {now} across "
                           f"the cut at {b.start_ms / 1000:g} s (the 180-degree rule)")  # fmt: skip
    if pace is not None:
        limit = pace_rule(pace).reaction_s
        for index, ms, away in reaction_holds(subs, spoken, shot.character_ids, pace):
            out.append(f"{LISTENER} {ms / 1000:g} s in sub-shot {index} while "
                       f"{', '.join(names.get(w, w) for w in away)} speaks off screen; at this "
                       f"pace a reaction holds at most {limit:g} s: put the speaker on screen "
                       "for their line")  # fmt: skip
    return out


def plan_problems(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, pace: str | None = None
) -> list[str]:
    """Where a shot's plan in force isn't a clean spec (with the story's `pace`, also a
    listener held longer than it allows). An unplanned shot (the single plan) is only
    flagged for what planning would answer or nothing can: dialogue held in one cut, and a
    shot too long for H3."""
    problems = check_plan(pkg, shot, plan.sub_shots, pace)
    return problems if plan.source != "single" else [p for p in problems if advisory(p)]


def plan_of(pkg: ProductionPackage, shot: Shot, stored: ShotPlan | None) -> ShotPlan:
    """The plan in force: the stored one when it has sub-shots, else the single one,
    which is busy while a first plan is being written (so nothing is drawn meanwhile)."""
    if stored is not None and stored.sub_shots:
        return stored
    plan = single_plan(pkg, shot)
    if stored is not None:
        plan.status = stored.status
    return plan


def current_sub(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, take: ShotTake
) -> SubShot | None:
    """The plan's sub-shot a take was drawn for, if the plan still has it unchanged. A
    take from before sub-shots was drawn for the whole shot: it matches the single plan."""
    sub = plan.sub(take.sub_shot) if take.sub_shot >= 1 else None
    drawn = take.spec or single_plan(pkg, shot).sub_shots[0]
    if sub is None:
        return None
    if drawn == sub:
        return sub
    # The single plan's clip once ended at the storyboard's length, not whole seconds: a
    # keyframe drawn for it then is still the same view.
    same_view = drawn.model_copy(update={"end_ms": sub.end_ms}) == sub
    return sub if plan.source == "single" and same_view else None


def make_sub_shots(
    pkg: ProductionPackage,
    shot: Shot,
    rows: list[dict[str, Any]],
    clip: int | None = None,
    *,
    strict: bool = True,
) -> list[SubShot]:
    """Sub-shots from (start_ms, framing, camera, action, people, placement, off_screen)
    rows over a clip of `clip` ms (default: the shot's, see clip_ms). Each ends where the
    next starts; the last at the clip's end. Raises ValueError when they can't be a plan
    at all (1 to MAX_SUB_SHOTS, the first at 0, each at least MIN_SUB_SHOT_MS, a framing
    and an action each, only the shot's people). With `strict`, also raises PlanProblems
    for every rule of check_plan it breaks except the advisory ones (a shot too long for H3,
    dialogue held in one cut); without, all of them are left for the caller to flag."""
    if not 1 <= len(rows) <= MAX_SUB_SHOTS:
        raise ValueError(f"a shot has 1 to {MAX_SUB_SHOTS} sub-shots")
    clip = clip or clip_ms(shot)
    starts = [int(r["start_ms"]) for r in rows]
    if starts[0] != 0:
        raise ValueError("the first sub-shot starts at 0 s")
    ends = [*starts[1:], clip]
    out = []
    for k, (row, start, end) in enumerate(zip(rows, starts, ends, strict=True), 1):
        if end - start < MIN_SUB_SHOT_MS:
            raise ValueError(
                f"sub-shot {k} is {max(0, end - start) / 1000:.1f} s; each runs at least "
                f"{MIN_SUB_SHOT_MS / 1000:.0f} s, in order, inside the clip's {clip / 1000:g} s"
            )
        framing, action = str(row.get("framing", "")).strip(), str(row.get("action", "")).strip()
        if not framing or not action:
            raise ValueError(f"sub-shot {k} needs a framing and an action")
        people = list(dict.fromkeys(row.get("people") or []))
        if stray := [p for p in people if p not in shot.character_ids]:
            raise ValueError(f"sub-shot {k} lists {', '.join(stray)}, who isn't in the shot")
        out.append(SubShot(index=k, start_ms=start, end_ms=end, framing=framing,
                           camera=str(row.get("camera", "")).strip(), action=action,
                           people=people, placement=dict(row.get("placement") or {}),
                           off_screen=dict(row.get("off_screen") or {})))  # fmt: skip
    if strict and (problems := [p for p in check_plan(pkg, shot, out) if not advisory(p)]):
        raise PlanProblems(problems)
    return out
