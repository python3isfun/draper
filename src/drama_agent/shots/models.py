"""Keyframe data: settings, references, sub-shots, plans, prompt versions, checks, draws
and takes, and how a check is scored."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..models.package import utcnow
from ..pace import Pace
from ..shot_reading import PersonState

WRITE_STAGE = "shot_frames"

CHECK_STAGE = "shot_check"

PLAN_STAGE = "shot_plan"

# The most cuts a shot holds (a person's plan, an inserted cut); the planner cuts a shot into
# at most PLANNER_MAX_SUB_SHOTS on its own, more only when asked for a count.
MAX_SUB_SHOTS = 5

PLANNER_MAX_SUB_SHOTS = 4

MIN_SUB_SHOT_MS = 1000

MIN_CLIP_S, MAX_CLIP_S = 4, 15  # H3 renders 4 to 15 s clips, in whole seconds

# A clip this long with two or more speakers is covered speaker by speaker, not held in one cut.
COVERAGE_MS = 6000

# The sub-shot spec's version in the export (05_assets/shot_keyframes/sub_shots.json): the
# contract video production writes its prompt from.
SPEC_VERSION = 1

Side = Literal["left", "center", "right"]

OffSide = Literal["left", "right"]

MAX_ROUNDS = 3  # automatic rounds before a person decides

MAX_DRAWS = 4

DEFAULT_DRAWS = 2

DEFAULT_PROVIDER = "h3"  # MiniMax H3 on MCPHub's GPU route; "nano-banana" when configured

PASS_SCORE = 80

MAJOR_PENALTY = 25

MINOR_PENALTY = 8

JUDGE_PX = 1024  # long edge of the draw the judge sees

NO_OTHER_TEXT = "No other lettering is added."

# "paused": stopped by the user between steps; resumed, it goes on where it stopped.
TakeStatus = Literal["pending", "writing", "drawing", "checking", "done", "failed", "paused"]

CheckKind = Literal[
    "identity", "wardrobe", "set", "prop", "count", "placement", "framing", "action",
    "defects", "text", "style",
]  # fmt: skip

# The deliverable a shot sends for each kind of requirement.
SHOT_VIEWS = {
    "character_look": "full body",
    "character_identity": "front face",
    "location": "wide",
    "prop": "detail",
}

# ---------------------------------------------------------------------------
# Models


class FrameRef(BaseModel):
    """One picture a frame is drawn from, as `<Picture N>` in its prompt."""

    ref_id: str  # a design image (img_...) or an approved keyframe (frame:<take>:<n>)
    role: str  # "Lila in this shot's look, full body", "the approved keyframe of sub-shot 1"
    # previous_frame: the approved keyframe of the sub-shot before ("start_frame": older
    # takes' name for it).
    # previous_shot: the approved keyframe of the shot before, in the same scene.
    kind: Literal["look", "face", "set", "prop", "previous_frame", "start_frame", "previous_shot"]
    entity_id: str | None = None  # the character, location or prop shown
    likeness: bool = False  # shows a real person's likeness (content rule 3)
    # For a person's picture chosen by reading the scene (shot_reading.py): how they look at
    # this moment, and what the picture already gets right.
    state: PersonState | None = None
    keep: list[str] = Field(default_factory=list)


class SubShot(BaseModel):
    """One cut inside a shot's video generation: `[Shot N]` of the video prompt."""

    index: int = Field(ge=1)  # 1-based, in time order
    start_ms: int = Field(ge=0)  # from the start of the shot (the clip)
    end_ms: int = Field(gt=0)
    framing: str
    camera: str = ""
    action: str
    people: list[str] = Field(default_factory=list)  # character ids on screen
    # Where each on-screen person is in the frame, and where each of the shot's other
    # people is off frame (for eyelines across the cut). Character id -> side.
    placement: dict[str, Side] = Field(default_factory=dict)
    off_screen: dict[str, OffSide] = Field(default_factory=dict)

    @property
    def timecode(self) -> str:
        """The video prompt's timestamp for this cut, e.g. "00:02.000"."""
        s = self.start_ms / 1000
        return f"{int(s // 60):02d}:{s % 60:06.3f}"


class ShotPlan(BaseModel):
    """How a shot is cut inside its one video generation. Stored once written (by the
    model or a person); a shot without one is one sub-shot (`single_plan`)."""

    package_id: str
    revision_id: str
    shot_id: str
    status: Literal["writing", "done", "failed"] = "done"
    # keyframes: locked, each cut's view as its approved keyframe shows it (lock.py).
    source: Literal["single", "model", "user", "keyframes"] = "single"
    # The clip length asked for when planning (None: the storyboard's, see clip_ms).
    clip_ms: int | None = None
    # This shot's own pace (None: the story's) and how many sub-shots it is cut into (None:
    # the planner decides). Asked for when planning; kept for the next re-plan.
    pace: Pace | None = None
    cuts: int | None = Field(None, ge=1, le=MAX_SUB_SHOTS)
    sub_shots: list[SubShot] = Field(default_factory=list)
    notes: str = ""
    model: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    # The design text it was planned against (design_print); None for plans from before it
    # was recorded (judged by the hand edits made to the shot's set, looks and props).
    design_print: str | None = None
    # Set on a plan written by inserting a cut: the cut it was inserted after (inserts.py),
    # so restoring the plan before it, or this one again, moves the keyframes with it.
    inserted_after: int | None = None

    @property
    def busy(self) -> bool:
        return self.status == "writing"

    def sub(self, index: int) -> SubShot | None:
        return next((s for s in self.sub_shots if s.index == index), None)


class PromptBlock(BaseModel):
    """One cut of a shot's video prompt: its text (pictures as `{pic:<id>}`) and its camera
    sentence, kept apart so video production can swap it."""

    sub_shot: int = Field(ge=1)
    text: str
    camera: str


class ShotVideoPrompt(BaseModel):
    """A shot's H3 video prompt (video_prompt.py), written by the text model from the plan,
    keyframes and scene reading in force, recorded as `written_from`: once they change it is
    out of date. Pictures are `{pic:<ref id>}`. `problems` make it unusable (it isn't
    exported); `advice` are weaker findings, kept for a person to judge."""

    package_id: str
    revision_id: str
    shot_id: str
    status: Literal["writing", "done", "failed"] = "writing"
    source: Literal["model", "template", "user"] = "model"
    blocks: list[PromptBlock] = Field(default_factory=list)
    sound: str = ""
    notes: str = ""
    problems: list[str] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    written_from: str = ""  # the fingerprint of its inputs (video_prompt.fingerprint)
    model: str | None = None
    error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def busy(self) -> bool:
        return self.status == "writing"


class ShotPlanVersion(BaseModel):
    """A shot's cut plan as it was before it was replaced (by the planner, a person or a
    restore), with the keyframes approved for it then ({take_id: draw n}), so it can be
    restored with them. Kept per story revision and moved with it."""

    package_id: str
    revision_id: str
    shot_id: str
    version: int  # 1, 2, ... per shot, in the order the plans were replaced
    plan: ShotPlan
    approved: dict[str, int] = Field(default_factory=dict)
    replaced_at: datetime = Field(default_factory=utcnow)
    replaced_by: Literal["model", "user", "restore", "lock"] = "model"


class PromptVersion(BaseModel):
    """One version of a sub-shot keyframe's prompt. The writer's prompts and a person's
    edits are versions; any version can be drawn, compared or restored."""

    version_id: str = Field(default_factory=lambda: f"spv_{uuid.uuid4().hex[:12]}")
    package_id: str
    revision_id: str
    shot_id: str
    sub_shot: int
    spec: SubShot  # the sub-shot as planned when this version was written
    v: int = 0  # 1, 2, ... per shot and sub-shot (set by the store)
    prompt: str
    source: Literal["model", "template", "user"]
    model: str | None = None
    note: str = ""  # the editor's note, or the writer's
    based_on: int | None = None  # the version this one edits
    take_id: str | None = None  # the take whose writer wrote it
    findings: list[str] = Field(default_factory=list)  # where it breaks the H3 image rules
    # The pictures it was written for, in <Picture N> order: drawing it sends these, so its
    # picture numbers still mean what they meant when it was written.
    references: list[FrameRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class CheckItem(BaseModel):
    item: str = Field(description='What was checked, e.g. "Lila: face" or "people count".')
    kind: CheckKind
    passed: bool
    severity: Literal["minor", "major"] = Field(
        "major",
        description="major: a viewer would notice, or the video would carry it (wrong face, "
        "wrong clothes, a missing or extra person, wrong side, wrong action, a deformed "
        "hand in view); minor: small and easy to live with.",
    )
    finding: str = Field("", description="What is wrong, concretely; empty when it passes.")


class FrameCheck(BaseModel):
    """The judge's verdict on one draw. `score` and `passed` are computed from the items."""

    items: list[CheckItem] = Field(default_factory=list)
    summary: str = ""
    fix: str = ""  # how the prompt should change, for the next round
    score: int = 0
    passed: bool = False
    model: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


class FrameDraw(BaseModel):
    n: int  # 0-based draw number within the take
    seed: int
    status: Literal["done", "failed"] = "done"
    file: str | None = None
    thumb: str | None = None
    view: str | None = None  # the downscaled copy the judge sees
    content_type: str = "image/jpeg"
    model: str = ""
    check: FrameCheck | None = None
    check_error: str | None = None
    error: str | None = None


class ShotTake(BaseModel):
    """One round of one sub-shot's keyframe: a prompt and its draws, each checked."""

    take_id: str
    package_id: str
    revision_id: str
    shot_id: str
    sub_shot: int = 1  # 0: an end frame from before sub-shots (not shown or exported)
    spec: SubShot | None = None  # the sub-shot as planned when this take was made
    round: int = 1
    run_id: str  # the first take of an automatic run; its later rounds share it
    status: TakeStatus = "pending"
    prompt: str = ""
    prompt_source: Literal["pending", "model", "template", "user"] = "pending"
    prompt_model: str | None = None
    notes: str = ""  # the writer's note to the user
    prompt_version: int | None = None  # the prompt version this take draws
    warnings: list[str] = Field(default_factory=list)  # H3 rule findings on its prompt
    references: list[FrameRef] = Field(default_factory=list)
    size: str
    # The image provider that draws it: "h3" (MiniMax H3) or "nano-banana" (Gemini).
    provider: str = DEFAULT_PROVIDER
    # The design text it was written and checked against (design_print); None for takes
    # from before it was recorded, which are judged by their pictures alone.
    design_print: str | None = None
    # Kept approved when a cut was inserted right before its cut (inserts.py): it continues
    # from the cut before the insert, so it is out of date until redrawn, but a new approval
    # of the inserted cut doesn't withdraw it.
    moved_by_insert: bool = False
    draws: int = DEFAULT_DRAWS
    seeds: list[int] = Field(default_factory=list)
    results: list[FrameDraw] = Field(default_factory=list)
    direction: str | None = None  # the user's steer for this take
    previous_prompt: str | None = None  # the prompt of the round this one revises
    fixes: list[str] = Field(default_factory=list)  # the judge's findings this round fixes
    auto: bool = True  # go on to another round when no draw passes
    max_rounds: int = MAX_ROUNDS
    motion: str = ""  # the writer's note on how the sub-shot moves after this frame
    approved: int | None = None  # the approved draw (one per shot and sub-shot)
    # A take brought in from an image chat ("experiment <session_id> #<n>"): one draw,
    # the chat's image, not drawn by the runner (docs/library-layers.md).
    imported_from: str | None = None
    likeness: bool = False
    refused: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_frames(cls, data: Any) -> Any:
        """Takes from before sub-shots had `frame`: a start frame is sub-shot 1, an end
        frame sub-shot 0 (kept, but no longer shown or exported)."""
        if isinstance(data, dict) and "frame" in data and "sub_shot" not in data:
            data = {**data, "sub_shot": 0 if data["frame"] == "end" else 1}
        return data

    @property
    def busy(self) -> bool:
        return self.status not in ("done", "failed", "paused")

    def best(self) -> FrameDraw | None:
        """The draw with the best check (passing first), if any was checked."""
        checked = [d for d in self.results if d.status == "done" and d.check is not None]
        return max(checked, key=lambda d: (d.check.passed, d.check.score), default=None)  # type: ignore[union-attr]

    def passed(self) -> bool:
        return any(d.check is not None and d.check.passed for d in self.results)


def score(items: list[CheckItem]) -> tuple[int, bool]:
    """(score out of 100, passed): every failed item costs points; any failed major item
    fails the draw whatever the score."""
    lost = sum(MAJOR_PENALTY if i.severity == "major" else MINOR_PENALTY
               for i in items if not i.passed)  # fmt: skip
    total = max(0, 100 - lost)
    major = any(not i.passed and i.severity == "major" for i in items)
    return total, bool(items) and not major and total >= PASS_SCORE
