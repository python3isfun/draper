"""Locking a shot: its approved keyframes become the plan.

Keyframes are made in two phases. In the first, the script's plan and prompts draft rough
keyframes, and an editor reviews the sequence (the Gallery), fixing, redrawing and adding
cuts until it looks right. In the second, the editor locks the shot: the vision model reads
each approved keyframe (`read_keyframes`) and writes down what it shows in the plan's own
words (shot size, camera position, who is where, what they wear and hold, the background);
the editor corrects that reading and confirms (`lock_shot`), and the plan's view fields are
replaced by it (`plan.source = "keyframes"`). From then on the keyframes are the source of
truth: the video prompt and the export are written from what was drawn, the draft plan is
in the plan history (restored by unlocking), and the original keyframe prompts are history.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm import GenerationRequest
from ..models import ProductionPackage
from ..models.package import utcnow
from ..models.storyboard import Shot
from .history import replace_plan
from .models import OffSide, ShotPlan, ShotTake, Side, SubShot
from .planner import _sides, _who
from .plans import current_sub, make_sub_shots
from .writer import shot_context, sub_shot_context

LOCK_STAGE = "keyframe_reading"


class SeenCut(BaseModel):
    """What one cut's approved keyframe shows, in the plan's fields."""

    index: int = Field(ge=1)
    framing: str
    camera: str = ""
    people: list[str] = Field(default_factory=list)  # character ids on screen
    placement: dict[str, Side] = Field(default_factory=dict)
    off_screen: dict[str, OffSide] = Field(default_factory=dict)
    # Clothes, props in hand, pose and background as drawn, for the video prompt.
    note: str = ""


class KeyframeReading(BaseModel):
    """A shot's approved keyframes as the vision model read them (lock.py): the proposal an
    editor corrects and confirms to lock the shot. `frames` records the keyframes read, so
    the reading is stale once one changes."""

    package_id: str
    revision_id: str
    shot_id: str
    status: Literal["writing", "done", "failed"] = "writing"
    frames: str = ""
    cuts: list[SeenCut] = Field(default_factory=list)
    # Continuity between the cuts and with the plan, for the editor's eye.
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def busy(self) -> bool:
        return self.status == "writing"


def frames_print(frames: dict[int, str | None]) -> str:
    """The approved keyframes a reading is of, as one string."""
    raw = "|".join(f"{i}:{frames[i] or ''}" for i in sorted(frames))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The reading


ROLE = """\
You read the approved keyframes of one shot of a short drama and write down what each \
shows, in the words a shot plan uses. The keyframes were drawn from a plan, and an editor \
has approved them as the shot's cuts: where a picture and the plan differ, the picture is \
right. Your reading replaces the plan's view of each cut, and the video prompt is written \
from it, so it must say what is in the picture, exactly and only.\
"""

GUIDE = """\
## Your input

- `shot`: the storyboard shot, its people (with their look) and the scene.
- `cuts`: each cut as planned (framing, camera, who is on screen and where, who is off \
screen, the action and lines), in time order. The approved keyframe of each cut is \
attached, labelled `<Cut N>`.

## What you write

One `cuts` entry per cut, in order, each with:

- `index`: the cut's number.
- `framing`: the shot size and composition as drawn, in one or two sentences: "Medium \
two-shot from the foot of the bed at eye height; Lila stands frame left in profile, \
Derek reclines frame right under an ivory sheet; the window and sheer curtains behind \
them." Say where the camera stands, its height and what it faces, as the picture shows.
- `camera`: the camera's movement from the plan (the picture can't show it), in a few \
words; "static" when the plan has none.
- `people`: the names of the people on screen, left to right.
- `placement`: each on-screen person's side of the frame: left, center or right.
- `off_screen`: each of the shot's other people who are off screen, and the side they are \
on (left or right) as the plan says or the picture implies (an eyeline).
- `note`: what the video prompt needs that the fields don't hold: each person's clothes, \
hair, what they hold, their pose and expression, props in frame, and what is behind them, \
as drawn. Plain description; no judgement.

Then `warnings`: one line each for anything in the sequence an editor would want to \
know: a person who swaps sides between cuts, a prop that appears or vanishes, clothes or \
hair that change, light or set dressing that changes, a cut that shows a different moment \
than its action. Empty when the cuts agree.

## Rules

- Describe the picture, not the plan. When the plan says close-up and the picture is a \
full-length shot, write full-length.
- Name only the shot's people; use their names as given.
- Describe only what is visible: no "should", "must" or "instead".
"""


class SeenOut(BaseModel):
    index: int
    framing: str
    camera: str = ""
    people: list[str] = Field(default_factory=list)
    placement: dict[str, str] = Field(default_factory=dict)
    off_screen: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class ReadingOutput(BaseModel):
    cuts: list[SeenOut]
    warnings: list[str] = Field(default_factory=list)


def lock_instructions() -> str:
    return ROLE + "\n\n" + GUIDE


def reading_context(pkg: ProductionPackage, shot: Shot, plan: ShotPlan) -> dict[str, Any]:
    base = shot_context(pkg, shot)
    base["shot"].pop("dialogue", None)
    return {
        **base,
        "cuts": [
            {
                **sub_shot_context(pkg, shot, sub, len(plan.sub_shots)),
                "picture": f"<Cut {sub.index}>",
            }
            for sub in plan.sub_shots
        ],
    }


def seen_cuts(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, out: ReadingOutput
) -> list[SeenCut]:
    """The model's reading as SeenCuts, names turned into ids and sides checked; a cut it
    didn't read keeps the plan's view. Raises ValueError for a name that is no one."""
    by_index = {c.index: c for c in out.cuts}
    cuts = []
    for sub in plan.sub_shots:
        c = by_index.get(sub.index)
        if c is None:
            cuts.append(SeenCut(index=sub.index, framing=sub.framing, camera=sub.camera,
                                people=sub.people, placement=sub.placement,
                                off_screen=sub.off_screen))  # fmt: skip
            continue
        people = list(dict.fromkeys(_who(pkg, shot, n) for n in c.people))
        cuts.append(SeenCut(
            index=sub.index,
            framing=c.framing.strip() or sub.framing,
            camera=c.camera.strip() or sub.camera,
            people=people,
            placement=_sides(pkg, shot, c.placement, ("left", "center", "right"),
                             "the frame position"),  # type: ignore[arg-type]
            off_screen=_sides(pkg, shot, c.off_screen, ("left", "right"),
                              "the off-screen side"),  # type: ignore[arg-type]
            note=c.note.strip(),
        ))  # fmt: skip
    return cuts


def fixture_reading(request: GenerationRequest) -> ReadingOutput:
    """Offline stand-in: each cut as planned, noted as read."""
    return ReadingOutput(
        cuts=[
            SeenOut(
                index=c["index"],
                framing=c["framing"],
                camera=c["camera"],
                people=[p["name"] for p in c["people"]],
                placement={p["name"]: p["frame"] for p in c["people"] if p["frame"]},
                off_screen={o["name"]: o["side"] for o in c["off_screen"]},
                note="Offline sample: read as planned.",
            )
            for c in request.context["cuts"]
        ],
        warnings=[],
    )


# ---------------------------------------------------------------------------
# Locking


def locked_subs(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, cuts: list[SeenCut]
) -> list[SubShot]:
    """The plan's cuts with each view replaced by what was seen (timing and action kept).
    Raises ValueError when `cuts` aren't one per cut of the plan."""
    if [c.index for c in cuts] != [s.index for s in plan.sub_shots]:
        raise ValueError("give one reading per cut of the plan, in order")
    rows = []
    for sub, c in zip(plan.sub_shots, cuts, strict=True):
        framing = c.framing.strip()
        if c.note.strip():
            framing = f"{framing.rstrip('.')}. {c.note.strip()}"
        rows.append({"start_ms": sub.start_ms, "framing": framing, "camera": c.camera.strip(),
                     "action": sub.action, "people": c.people, "placement": c.placement,
                     "off_screen": c.off_screen})  # fmt: skip
    return make_sub_shots(pkg, shot, rows, plan.sub_shots[-1].end_ms, strict=False)


def lock_shot(
    repo: Any, pkg: ProductionPackage, shot: Shot, plan: ShotPlan, cuts: list[SeenCut]
) -> ShotPlan:
    """Make the keyframes' reading the shot's plan: the plan in force goes to the history
    (replaced by "lock"), the shot's takes keep their approvals (their spec follows the
    cut they were drawn for), and the new plan is `source: keyframes`."""
    subs = locked_subs(pkg, shot, plan, cuts)
    new = plan.model_copy(update={
        "sub_shots": subs, "source": "keyframes", "status": "done", "error": None,
        "notes": "Locked: each cut as its approved keyframe shows it.",
        "updated_at": utcnow(), "inserted_after": None,
    })  # fmt: skip
    takes: list[ShotTake] = repo.list_shot_takes(pkg.package_id, pkg.revision_id)
    for t in takes:
        if t.shot_id == shot.shot_id and current_sub(pkg, shot, plan, t) is not None:
            spec = new.sub(t.sub_shot)
            if spec is not None:
                t.spec = spec
                repo.save_shot_take(t)
    replace_plan(repo, pkg, shot, new, "lock", previous=plan)
    return new


def unlock_shot(
    repo: Any, pkg: ProductionPackage, shot: Shot, plan: ShotPlan
) -> tuple[ShotPlan, int]:
    """Restore the draft plan the lock replaced; the shot's takes follow their cut back
    (their spec was the locked view), so their approvals hold. Raises KeyError when the
    draft is no longer in the history, ValueError when it no longer fits the shot."""
    from .history import restore_plan

    version = lock_version(repo, pkg, shot)
    if version is None:
        raise KeyError("draft")
    draft = next(v.plan for v in repo.list_plan_versions(pkg.package_id, pkg.revision_id,
                                                         shot.shot_id) if v.version == version)  # fmt: skip
    for t in repo.list_shot_takes(pkg.package_id, pkg.revision_id):
        if t.shot_id == shot.shot_id and current_sub(pkg, shot, plan, t) is not None:
            spec = draft.sub(t.sub_shot)
            if spec is not None:
                t.spec = spec
                repo.save_shot_take(t)
    return restore_plan(repo, pkg, shot, version)


def lock_version(repo: Any, pkg: ProductionPackage, shot: Shot) -> int | None:
    """The version of the draft plan a lock replaced, to restore on unlock."""
    locks = [v for v in repo.list_plan_versions(pkg.package_id, pkg.revision_id, shot.shot_id)
             if v.replaced_by == "lock"]  # fmt: skip
    return max((v.version for v in locks), default=None)
