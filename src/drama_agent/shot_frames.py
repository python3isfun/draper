"""Shot keyframes: a checked picture per sub-shot, made from the design picks, for video.

The video model keeps characters consistent and moves them as intended only as well as its
inputs show it. A storyboard shot is one video generation, but a dynamic one holds two or
three cuts inside it (0-2 s a wide, 2-6 s a close-up), which the video prompt marks as
`[Shot 2] At 00:02.000, the camera cuts to ...`. So before a shot goes to video production
(dramer), each shot gets:

1. **A sub-shot plan**: the text model splits the shot into 1-3 sub-shots (H3's rules: cut
   only for new information; timestamps strictly increasing inside the clip), each with its
   start time, framing, camera, action and who is on screen. A person can edit it. Without
   one, the whole shot is one sub-shot.
2. **A keyframe per sub-shot**: the first frame of that sub-shot, an H3 edit that puts its
   people, in their picked looks, into the picked set with its props. Sub-shot 1 is drawn
   from the design picks; each later one also from the approved keyframe of the sub-shot
   before it (`<Picture 1>`), so light, wardrobe state and set dressing carry across the cut.

Each keyframe goes through a quality loop:

1. **Write**: the text model writes the edit prompt from the sub-shot and the reference
   pictures (`<Picture N>`).
2. **Draw**: a few draws with chosen or random seeds.
3. **Check**: a vision judge compares each draw with the reference pictures and the
   sub-shot, item by item (each person's face, hair and wardrobe; the set and props; how
   many people and where; framing; the action; defects; stray text). The score and the
   verdict are computed here from the items, not taken from the judge.
4. **Revise**: if no draw passes, the failed items and the judge's fix go back to the
   writer for the next round, with the same seeds so rounds compare like for like; up to
   `MAX_ROUNDS` rounds, then a person decides.

A person approves one draw per sub-shot; approving one starts the next sub-shot. Only
approved keyframes of the current plan are exported, with the plan's timestamps, so the
video prompt cites each as the first frame of its `[Shot N]`. Keyframes are production
assets, so the restrictive content rules always apply (as on the design board), and a
shot in a scene staged as implied or simulated intimacy is never drawn from a pick that
shows a real person's likeness (content rule 3).

The code lives in the `shots` package now; this module re-exports it
under the old names."""

from .imagegen import MAX_REFERENCES  # passed along, as before the split
from .shots.approvals import (
    approval_changes,
    approved_take,
    frame_ref,
    is_busy,
    stale_approvals,
)
from .shots.board import (
    shot_board,
)
from .shots.common import (
    _REVISION_CACHE,
    KEYFRAME_SIZES,
    NotReady,
    _cached,
    _dialogue,
    _names,
    frame_size,
    intimate_scene,
    scene_of,
    shot_of,
)
from .shots.judge import (
    JUDGE_GUIDE,
    JUDGE_ROLE,
    JudgeOutput,
    fixture_check,
    judge_instructions,
)
from .shots.models import (
    CHECK_STAGE,
    COVERAGE_MS,
    DEFAULT_DRAWS,
    DEFAULT_PROVIDER,
    JUDGE_PX,
    MAJOR_PENALTY,
    MAX_CLIP_S,
    MAX_DRAWS,
    MAX_ROUNDS,
    MAX_SUB_SHOTS,
    MIN_CLIP_S,
    MIN_SUB_SHOT_MS,
    MINOR_PENALTY,
    NO_OTHER_TEXT,
    PASS_SCORE,
    PLAN_STAGE,
    SHOT_VIEWS,
    SPEC_VERSION,
    WRITE_STAGE,
    CheckItem,
    CheckKind,
    FrameCheck,
    FrameDraw,
    FrameRef,
    OffSide,
    PromptVersion,
    ShotPlan,
    ShotTake,
    Side,
    SubShot,
    TakeStatus,
    score,
)
from .shots.picks import (
    ShotPicks,
    _req_title,
    shot_picks,
    shot_references,
)
from .shots.planner import (
    PLAN_GUIDE,
    PLAN_ROLE,
    PlannedSubShot,
    PlanOutput,
    _sides,
    _who,
    fixture_plan,
    plan_context,
    plan_instructions,
    planned_sub_shots,
    spread,
)
from .shots.plans import (
    _HOLD_TWO_SHOT,
    ONE_CUT,
    TOO_LONG,
    PlanProblems,
    advisory,
    check_plan,
    clip_ms,
    coverage_cut,
    current_sub,
    holds_two_shot,
    make_sub_shots,
    plan_of,
    plan_problems,
    shot_lines,
    single_plan,
    sub_shot_lines,
)
from .shots.references import (
    chosen_pictures,
    previous_shot_frame,
    sub_shot_references,
)
from .shots.runner import (
    ShotFrameRunner,
    judge_view,
)
from .shots.staleness import (
    KF_BEFORE_CHANGED,
    KF_BEFORE_OUTDATED,
    KF_TEXT_CHANGED,
    PLAN_TEXT_CHANGED,
    design_ids,
    design_print,
    edited_since,
    keyframe_judge,
    outdated_keyframes,
    plan_outdated,
)
from .shots.takes import (
    _fixes,
    _seeds,
    next_round,
    plan_take,
    sub_shot_for,
)
from .shots.writer import (
    _COMMAND,
    _NEGATION,
    _QUOTED,
    _STORY,
    KEYFRAME_RULE,
    WRITER_GUIDE,
    WRITER_ROLE,
    FramePromptOutput,
    fixture_frame_prompt,
    is_hard,
    picture_sentences,
    prompt_findings,
    shot_context,
    sub_shot_context,
    take_states,
    take_sub,
    template_prompt,
    worth_repair,
    writer_instructions,
)

__all__ = [
    "CHECK_STAGE",
    "COVERAGE_MS",
    "DEFAULT_DRAWS",
    "DEFAULT_PROVIDER",
    "JUDGE_GUIDE",
    "JUDGE_PX",
    "JUDGE_ROLE",
    "KEYFRAME_RULE",
    "KEYFRAME_SIZES",
    "KF_BEFORE_CHANGED",
    "KF_BEFORE_OUTDATED",
    "KF_TEXT_CHANGED",
    "MAJOR_PENALTY",
    "MAX_CLIP_S",
    "MAX_DRAWS",
    "MAX_REFERENCES",
    "MAX_ROUNDS",
    "MAX_SUB_SHOTS",
    "MINOR_PENALTY",
    "MIN_CLIP_S",
    "MIN_SUB_SHOT_MS",
    "NO_OTHER_TEXT",
    "ONE_CUT",
    "PASS_SCORE",
    "PLAN_GUIDE",
    "PLAN_ROLE",
    "PLAN_STAGE",
    "PLAN_TEXT_CHANGED",
    "SHOT_VIEWS",
    "SPEC_VERSION",
    "TOO_LONG",
    "WRITER_GUIDE",
    "WRITER_ROLE",
    "WRITE_STAGE",
    "_COMMAND",
    "_HOLD_TWO_SHOT",
    "_NEGATION",
    "_QUOTED",
    "_REVISION_CACHE",
    "_STORY",
    "CheckItem",
    "CheckKind",
    "FrameCheck",
    "FrameDraw",
    "FramePromptOutput",
    "FrameRef",
    "JudgeOutput",
    "NotReady",
    "OffSide",
    "PlanOutput",
    "PlanProblems",
    "PlannedSubShot",
    "PromptVersion",
    "ShotFrameRunner",
    "ShotPicks",
    "ShotPlan",
    "ShotTake",
    "Side",
    "SubShot",
    "TakeStatus",
    "_cached",
    "_dialogue",
    "_fixes",
    "_names",
    "_req_title",
    "_seeds",
    "_sides",
    "_who",
    "advisory",
    "approval_changes",
    "approved_take",
    "check_plan",
    "chosen_pictures",
    "clip_ms",
    "coverage_cut",
    "current_sub",
    "design_ids",
    "design_print",
    "edited_since",
    "fixture_check",
    "fixture_frame_prompt",
    "fixture_plan",
    "frame_ref",
    "frame_size",
    "holds_two_shot",
    "intimate_scene",
    "is_busy",
    "is_hard",
    "judge_instructions",
    "judge_view",
    "keyframe_judge",
    "make_sub_shots",
    "next_round",
    "outdated_keyframes",
    "picture_sentences",
    "plan_context",
    "plan_instructions",
    "plan_of",
    "plan_outdated",
    "plan_problems",
    "plan_take",
    "planned_sub_shots",
    "previous_shot_frame",
    "prompt_findings",
    "scene_of",
    "score",
    "shot_board",
    "shot_context",
    "shot_lines",
    "shot_of",
    "shot_picks",
    "shot_references",
    "single_plan",
    "spread",
    "stale_approvals",
    "sub_shot_context",
    "sub_shot_for",
    "sub_shot_lines",
    "sub_shot_references",
    "take_states",
    "take_sub",
    "template_prompt",
    "worth_repair",
    "writer_instructions",
]
