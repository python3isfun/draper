"""The keyframe board: every shot with its plan, reading, and per sub-shot its pictures,
takes, versions and approval."""

from __future__ import annotations

from typing import Any

from .. import shot_reading
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..shot_reading import SceneReading
from .approvals import approved_take
from .clip_refs import exportable
from .common import _names, intimate_scene
from .models import PromptVersion, ShotPlan, ShotTake, ShotVideoPrompt
from .picks import shot_picks
from .plans import current_sub, plan_of, plan_problems, shot_lines, sub_shot_lines
from .references import sub_shot_references
from .staleness import keyframe_judge, outdated_keyframes, plan_outdated
from .video_prompt import (
    approved_frames,
    carried_lines,
    dialogue_pace,
    fingerprint,
    prompt_inputs,
    prompt_state,
    word_count,
)

# ---------------------------------------------------------------------------
# Board view


def shot_board(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    takes: list[ShotTake],
    plans: list[ShotPlan],
    versions: list[PromptVersion] | None = None,
    readings: list[SceneReading] | None = None,
    pace: str | None = None,
    video_prompts: list[ShotVideoPrompt] | None = None,
) -> list[dict[str, Any]]:
    """Every shot with its sub-shot plan and its scene's reading, and per sub-shot what it
    draws from (each person's chosen picture, state and why), what is missing, its prompt
    versions, its takes and its approved keyframe. With the story's `pace`, a plan's
    problems include a listener held longer than it allows. Each shot also has its video
    prompt (video_prompt.py): how it stands against the shot as it is now, and its text."""
    names = _names(pkg)
    stored = {p.shot_id: p for p in plans}
    judge = keyframe_judge(pkg, images, takes)
    stale = outdated_keyframes(pkg, images, takes, plans, judge)
    by_scene = {r.scene_id: r for r in readings or []}
    fresh_of: dict[str, SceneReading | None] = {}
    for scene_id in {s.scene_id for s in pkg.shot_list}:
        print_ = shot_reading.fingerprint(pkg, scene_id, stored, images)
        fresh_of[scene_id] = shot_reading.fresh(by_scene.get(scene_id), print_)
    prompts = {p.shot_id: p for p in video_prompts or []}
    usable = exportable(images)
    carried = carried_lines(pkg)
    out = []
    for shot in pkg.shot_list:
        reading = fresh_of.get(shot.scene_id)
        pool = {c.image_id: c for c in reading.candidates} if reading else {}
        own = stored.get(shot.shot_id)
        plan = plan_of(pkg, shot, own)
        mine = sorted((t for t in takes if t.shot_id == shot.shot_id), key=lambda t: t.created_at)
        picks = shot_picks(pkg, images, shot)  # once per shot, chosen per sub-shot below
        lines = shot_lines(pkg, shot)
        subs = []
        for sub in plan.sub_shots:
            # Every take: the previous shot's keyframe is among them.
            refs, missing = sub_shot_references(pkg, images, takes, shot, sub, picks, reading,
                                                judge)  # fmt: skip
            choices = []
            for who in sub.people:
                c = reading.choice(shot.shot_id, sub.index, who) if reading else None
                if c is None:
                    continue
                choices.append({
                    "character_id": who,
                    "name": names.get(who, who),
                    "image_id": c.image_id,
                    "state": c.state.model_dump(),
                    "keep": c.keep,
                    "reason": c.reason,
                    "source": c.source,
                    "shows": pool[c.image_id].shows if c.image_id in pool else "",
                    # Shows a real person's likeness: the UI labels it "Lookalike".
                    "likeness": pool[c.image_id].likeness if c.image_id in pool else False,
                    "candidates": [{"image_id": x.image_id, "label": x.label, "shows": x.shows,
                                    "likeness": x.likeness}
                                   for x in pool.values() if x.character_id == who],
                })  # fmt: skip
            ap = approved_take(mine, shot.shot_id, sub.index)
            subs.append({
                # Why the approved keyframe is out of date (empty when it isn't).
                "outdated": stale.get(ap.take_id, []) if ap is not None else [],
                **sub.model_dump(),
                "timecode": sub.timecode,
                "people_names": [names.get(c, c) for c in sub.people],
                "lines": sub_shot_lines(lines, sub),
                "references": [r.model_dump() for r in refs],
                "missing": missing,
                # Takes drawn for this sub-shot as it is now (not for an older plan's).
                "takes": [t.model_dump(mode="json") for t in mine
                          if t.sub_shot == sub.index and current_sub(pkg, shot, plan, t)],
                "approved": [ap.take_id, ap.approved] if ap is not None else None,
                # The number the next saved version gets (numbering runs across plans).
                "next_version": 1 + max((v.v for v in versions or []
                                         if (v.shot_id, v.sub_shot) == (shot.shot_id, sub.index)),
                                        default=0),
                # Prompt versions written for this sub-shot as it is now, oldest first.
                "choices": choices,
                "versions": [v.model_dump(mode="json") for v in versions or []
                             if (v.shot_id, v.sub_shot, v.spec) == (shot.shot_id, sub.index, sub)],
            })  # fmt: skip
        out.append({
            "shot_id": shot.shot_id,
            "episode_id": shot.episode_id,
            "scene_id": shot.scene_id,
            "framing": shot.framing,
            "camera": shot.camera,
            "action": shot.visual_action,
            "duration_ms": shot.duration_ms,
            "people": [{"id": c, "name": names.get(c, c)} for c in shot.character_ids],
            "location_id": shot.location_id,
            "place": names.get(shot.location_id, shot.location_id),
            "intimacy": intimate_scene(pkg, shot),
            "clip_ms": plan.sub_shots[-1].end_ms,
            # The scene's reading of who looks how (shot_reading.py): fresh, or not yet.
            "reading": {
                "scene_id": shot.scene_id,
                "status": (r := by_scene.get(shot.scene_id)) and r.status,
                "fresh": reading is not None,
                "error": r.error if r else None,
                "model": r.model if r else None,
            },
            "plan": {
                "status": own.status if own else "done",
                "source": plan.source,
                # Where the plan isn't a clean spec for video production yet.
                "problems": plan_problems(pkg, shot, plan, pace),
                # A stored plan written before a design edit (its framing may describe it).
                "outdated": plan_outdated(pkg, shot, own),
                "notes": own.notes if own else "",
                "error": own.error if own else None,
                # The shot's own pace (None: the story's, `story_pace`) and cut count (None:
                # the planner decides), for the next plan.
                "pace": plan.pace,
                "story_pace": pace,
                "cuts": plan.cuts,
            },
            "sub_shots": subs,
            "video_prompt": video_prompt_view(
                pkg, shot, plan, takes, images, reading, prompts.get(shot.shot_id), usable,
                carried,
            ),
            "busy": (own is not None and own.busy) or any(t.busy for t in mine),
        })  # fmt: skip
    return out


def video_prompt_view(
    pkg: ProductionPackage,
    shot: Any,
    plan: ShotPlan,
    takes: list[ShotTake],
    images: list[GeneratedImage],
    reading: SceneReading | None,
    stored: ShotVideoPrompt | None,
    usable: set[str],
    carried: set[tuple[str, str]],
) -> dict[str, Any]:
    """A shot's video prompt for the board: its state against the shot now (none, writing,
    failed, current, outdated), its text and findings, a label for each picture it names
    (by ref id), the shot's dialogue-pace warning, and whether every cut has its approved
    keyframe (when it is written on its own)."""
    context, ids = prompt_inputs(pkg, shot, plan, takes, images, reading, usable, carried)
    labels = {}
    for p in context["pictures"]:
        ref = ids[p["pic"][5:-1]]
        kind = {"chosen": "look", "keyframe": "keyframe"}.get(p["kind"], p["kind"])
        labels[ref] = (f"keyframe of cut {p['cuts'][0]}" if p["kind"] == "keyframe" and p["cuts"]
                       else f"{p['of']} \u00b7 {kind}" if p["of"] else kind)  # fmt: skip
    frames = approved_frames(pkg, shot, plan, takes)
    view: dict[str, Any] = {
        "state": prompt_state(stored, fingerprint(context, ids)),
        "pictures": labels,
        "pace": dialogue_pace(pkg, shot, plan.sub_shots[-1].end_ms, carried),
        "keyframes_done": bool(frames) and all(frames.values()),
    }
    if stored is not None:
        view |= stored.model_dump(mode="json", include={
            "blocks", "sound", "notes", "problems", "advice", "error", "model", "updated_at"})  # fmt: skip
        view["words"] = word_count(stored.blocks, stored.sound)
    return view
