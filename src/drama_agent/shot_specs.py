"""Shot specs for video production: per shot, everything dramer makes the clip from.

`03_storyboard/shot_specs.json` (spec_version 3) is the hand-off contract. Each shot's
entry is complete: dramer stores `spec` as the shot's spec, sends `references` (in order,
up to its picture limit) with the shot, files `cast` as the shot's members, and renders
the clip from `prompt`. Draper decides all of it, because it holds what decides it: the
cut plan, the approved keyframes, the scene reading, and the pictures each keyframe was
drawn from.

`prompt` (new in version 3) is the shot's H3 video prompt (shots/video_prompt.py), in
format "h3-paragraph/1": one block per cut (`sub_shot`, `text`, `camera`, no timestamps:
dramer writes "[Shot N] At <timecode>" from the cut's `timecode`), `sound` (the last
sentence) and `notes`. Pictures are named `{pic:<file name>}`, the file's name as in
`references`; dramer turns each into `<Picture N>` in the order it sends them and drops
the sentences of a picture it doesn't send. A shot has a prompt only when it was written
from the shot as exported (plan, keyframes, pictures) and passed every hard check; else
`prompt` is null and `not_ready` says why. Each reference says whether the prompt
`named` it.

The references are in priority order (shots/clip_refs.py): the keyframes, the faces, then
the rest; at most VIDEO_MAX_REFERENCES, the others in `references_left_out`, so a
consumer with a smaller limit drops from the end. The keyframes are references, not
start frames: the video model matches each cut to its keyframe.

`readiness` says how far the keyframe pass has got: how many cuts have an approved
keyframe, how many shots are ready, and whether keyframe work (a plan being written, a
keyframe being drawn or checked, a prompt being written) is still running, so production
knows not to render from a revision that isn't finished. `pace` is the story's pace
(pace.py). A shot's `warnings` are worth a look but don't stop it: more spoken words than
its clip holds at a slow read.

All file paths are relative to the export's root folder.
"""

from __future__ import annotations

import json
from typing import Any

from .models import ProductionPackage
from .models.images import GeneratedImage
from .shot_frames import (
    ShotPlan,
    chosen_pictures,
    plan_of,
    plan_problems,
    shot_lines,
    sub_shot_lines,
)
from .shot_reading import SceneReading
from .shots.clip_refs import VIDEO_MAX_REFERENCES, clip_references
from .shots.models import ShotVideoPrompt
from .shots.video_prompt import (
    PIC,
    PROMPT_FORMAT,
    carried_lines,
    dialogue_pace,
    fingerprint,
    prompt_context,
    prompt_state,
)

SHOT_SPECS_VERSION = 3
SHOT_SPECS_PATH = "03_storyboard/shot_specs.json"
IMAGES = "05_assets/reference_images"
KEYFRAMES = "05_assets/shot_keyframes"


def shot_specs(
    pkg: ProductionPackage,
    stored: dict[str, ShotPlan],
    keyframes: dict[tuple[str, int], str],
    names: Any,
    readings: dict[str, SceneReading | None],
    images: list[GeneratedImage],
    exported: dict[str, str],
    outdated: dict[tuple[str, int], list[str]] | None = None,
    working: set[str] | None = None,
    pace: str | None = None,
    frame_ids: dict[tuple[str, int], str] | None = None,
    prompts: dict[str, ShotVideoPrompt] | None = None,
) -> dict[str, Any]:
    """The shot specs of every shot in shot-list order. `keyframes` maps (shot, sub-shot)
    to its approved keyframe's file in shot_keyframes/ and `frame_ids` to its ref id
    (frame:<take>:<n>); `readings` each scene's fresh reading (or None); `exported` each
    exported design image to its file in reference_images/; `working` the shots whose
    plan, keyframes or prompt are being made now; `prompts` each shot's stored video
    prompt."""
    bible = pkg.story_bible
    looks = {x.look_id: x for x in bible.looks}
    loc_states = {x.location_state_id: x for x in bible.location_states}
    prop_states = {x.prop_state_id: x for x in bible.prop_states}
    set_states = {x.location_state_id: x for x in (pkg.set_sheet.states if pkg.set_sheet else [])}
    script = {b.dialogue.dialogue_id: b.dialogue for ep in pkg.episodes for sc in ep.scenes
              for b in sc.beats if b.dialogue}  # fmt: skip
    episode_no = {ep.episode_id: ep.number for ep in pkg.episodes}

    # A keyframe without its ref id (a caller that has only files) goes by its file.
    frame_ids = {k: (frame_ids or {}).get(k) or f"file:{name}" for k, name in keyframes.items()}
    file_of = {frame_ids[k]: f"{KEYFRAMES}/{name}" for k, name in keyframes.items()}
    file_of |= {ref: f"{IMAGES}/{name}" for ref, name in exported.items()}
    carried_ids = carried_lines(pkg)

    def file_ref(r: dict[str, Any]) -> dict[str, Any]:
        return {"file": file_of[r["ref_id"]], "kind": r["kind"], "entity_id": r["entity_id"],
                "name": names.any(r["entity_id"]) if r["entity_id"] else "",
                "sub_shots": r["sub_shots"], "likeness": r["likeness"], "about": r["about"]}  # fmt: skip

    out = []
    for ep in pkg.episodes:
        # A line that runs over a cut is spoken in the shot it starts in; the next shot
        # only notes that it carries over.
        spoken: set[str] = set()
        shots = sorted((s for s in pkg.shot_list if s.episode_id == ep.episode_id),
                       key=lambda s: s.start_ms)  # fmt: skip
        for shot in shots:
            dialogue, carried, lost = [], [], []
            for cue in shot.dialogue_cues:
                line = script.get(cue.dialogue_id)
                if line is None:
                    lost.append(cue.dialogue_id)
                    continue
                who = names.any(line.character_id)
                if cue.dialogue_id in spoken:
                    carried.append({"id": cue.dialogue_id, "speaker_name": who,
                                    "text": line.text})  # fmt: skip
                    continue
                spoken.add(cue.dialogue_id)
                dialogue.append({"id": cue.dialogue_id, "character_id": line.character_id,
                                 "speaker_name": who, "text": line.text,
                                 "delivery": line.delivery})  # fmt: skip

            plan = plan_of(pkg, shot, stored.get(shot.shot_id))
            problems = plan_problems(pkg, shot, plan, pace)
            frames = {sub.index: keyframes.get((shot.shot_id, sub.index)) for sub in plan.sub_shots}
            # A plan nobody made (one cut, the storyboard's) is kept only with a keyframe.
            kept = plan.source != "single" or any(frames.values())
            all_frames = kept and all(frames.values())
            reading = readings.get(shot.scene_id)
            chosen = {sub.index: chosen_pictures(pkg, shot, sub, reading) for sub in plan.sub_shots}
            # The pictures in the order the clip sends them (clip_refs.py), as files.
            ids_of = {i: frame_ids.get((shot.shot_id, i)) for i in frames} if kept else {}
            sent, left, notes = clip_references(
                pkg, shot, plan, ids_of, reading, images, exported.__contains__
            )
            refs = [file_ref(r) for r in sent]
            left_out = [file_ref(r) for r in left]
            # The video prompt, where it was written from the shot as exported here.
            context, short = prompt_context(pkg, shot, plan, ids_of, reading, images,
                                            exported.__contains__, carried_ids)  # fmt: skip
            vp = (prompts or {}).get(shot.shot_id)
            state = prompt_state(vp, fingerprint(context, short))
            prompt, prompt_issue = None, None
            if vp is None or state == "none":
                prompt_issue = "it has no video prompt yet"
            elif state == "writing":
                prompt_issue = "its video prompt is being written"
            elif state == "failed":
                prompt_issue = "its video prompt couldn't be written" + (
                    f": {vp.error}" if vp.error else ""
                )
            elif state == "outdated":
                prompt_issue = ("its video prompt is out of date: the plan, keyframes or "
                                "pictures changed since it was written")  # fmt: skip
            elif vp.problems:
                prompt_issue = f"its video prompt fails a check: {'; '.join(vp.problems)}"
            else:
                prompt = export_prompt(
                    vp, {r["ref_id"]: file_of[r["ref_id"]] for r in [*sent, *left]}
                )
                if prompt is None:
                    prompt_issue = "its video prompt names a picture the export doesn't carry"
            named = set(PIC.findall(json.dumps(prompt))) if prompt else set()
            for r in [*refs, *left_out]:
                r["named"] = r["file"].rsplit("/", 1)[-1] in named if prompt else None

            # Who and what is in the shot, each with a note: a person as the reading found
            # them in their first cut, else their storyboard look.
            look_of = {r.character_id: r.look_id for r in shot.character_look_refs}
            cast = [{"entity_id": who, "kind": "character", "name": names.any(who),
                     "note": notes.get(who) or getattr(looks.get(look_of.get(who, "")),
                                                       "description", "")}
                    for who in shot.character_ids]  # fmt: skip
            loc = loc_states.get(shot.location_state_id)
            cast.append({"entity_id": shot.location_id, "kind": "location",
                         "name": names.any(shot.location_id),
                         "note": loc.description if loc else ""})  # fmt: skip
            for p in shot.prop_state_refs:
                st = prop_states.get(p.prop_state_id)
                cast.append({"entity_id": p.prop_id, "kind": "prop", "name": names.any(p.prop_id),
                             "note": st.description if st else ""})  # fmt: skip

            lines = shot_lines(pkg, shot)
            sub_shots = [
                {
                    "index": sub.index,
                    "start_ms": sub.start_ms,
                    "end_ms": sub.end_ms,
                    "timecode": sub.timecode,
                    "framing": sub.framing,
                    "camera": sub.camera,
                    "action": sub.action,
                    "people": [names.any(c) for c in sub.people],
                    "placement": {names.any(c): side for c, side in sub.placement.items()},
                    "off_screen": {names.any(c): side for c, side in sub.off_screen.items()},
                    "lines": [
                        {"speaker": x["speaker"], "line": x["text"], "start_ms": x["start_ms"],
                         "end_ms": x["end_ms"], "where": x["where"]}
                        for x in sub_shot_lines(lines, sub)
                    ],
                    "keyframe": f"{KEYFRAMES}/{frames[sub.index]}" if frames[sub.index] else None,
                    "now": {names.any(who): r.state.model_dump()
                            for who, r in chosen[sub.index].items() if r.state is not None},
                }
                for sub in plan.sub_shots
            ]  # fmt: skip
            over = [
                f'{c["speaker_name"]} is finishing "{c["text"]}" over the cut.' for c in carried
            ]
            continuity = " ".join(x for x in (shot.continuity.strip(), *over,
                                              shot.generation_notes.strip()) if x)  # fmt: skip
            not_ready = [*problems]
            not_ready += [f"dialogue {d} is not in the script" for d in lost]
            not_ready += [
                f"[Shot {i}] has no approved keyframe" for i, f in frames.items() if not f
            ]
            # An approved keyframe drawn before a design change (outdated_keyframes).
            not_ready += [f"[Shot {i}]'s keyframe is out of date: {'; '.join(why)}"
                          for i, f in frames.items()
                          if f and (why := (outdated or {}).get((shot.shot_id, i)))]  # fmt: skip
            if shot.shot_id in (working or set()):
                not_ready.append("its keyframe work is still running")
            if prompt_issue:
                not_ready.append(prompt_issue)
            clip_ms = plan.sub_shots[-1].end_ms if kept else shot.duration_ms
            st = set_states.get(shot.location_state_id)
            out.append({
                "shot_id": shot.shot_id,
                "episode": episode_no.get(shot.episode_id),
                "scene_id": shot.scene_id,
                "ready": not not_ready,
                "not_ready": not_ready,
                "warnings": [w for w in [dialogue_pace(pkg, shot, clip_ms, carried_ids)] if w],
                "keyframe_work_running": shot.shot_id in (working or set()),
                "keyframe_first": all_frames,
                "planned_by": plan.source,
                # Locked: each cut's view is what its approved keyframe shows (shots/lock.py).
                "locked": plan.source == "keyframes",
                # The pace this shot was planned at: its own, else the story's.
                "pace": plan.pace or pace,
                "spec": {
                    "summary": shot.narrative_function,
                    "action": shot.visual_action,
                    "camera": {"shot_size": shot.framing, "angle": "", "movement": shot.camera},
                    "lighting": st.lighting if st else "",
                    "target_duration_ms": clip_ms,
                    "dialogue": dialogue,
                    # Lines spoken in the shot before, running over the cut into this one.
                    "carried": carried,
                    "sound_notes": shot.sound,
                    "continuity_notes": continuity,
                    "sub_shots": sub_shots if kept else None,
                },
                "references": refs,
                "references_left_out": left_out,
                "cast": cast,
                "prompt": prompt,
            })  # fmt: skip
    order = {s.shot_id: i for i, s in enumerate(pkg.shot_list)}
    out.sort(key=lambda s: order.get(s["shot_id"], len(order)))
    return {"spec_version": SHOT_SPECS_VERSION, "max_references": VIDEO_MAX_REFERENCES,
            "pace": pace, "readiness": readiness(out), "shots": out}  # fmt: skip


def readiness(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """How far the keyframe pass has got, over the shot specs, with one sentence for
    production: still running (don't render yet), complete, or what is missing."""
    cuts = [sub for s in shots for sub in (s["spec"]["sub_shots"] or [])]
    framed = sum(1 for sub in cuts if sub["keyframe"])
    unplanned = sum(1 for s in shots if s["spec"]["sub_shots"] is None)
    ready = sum(1 for s in shots if s["ready"])
    running = sum(1 for s in shots if s["keyframe_work_running"])
    if not shots:
        note = "There are no shots: the storyboard has not been made."
    elif running:
        note = (f"The keyframe pass is still running on {running} shot(s): don't render from "
                "this revision yet; export again when it is done.")  # fmt: skip
    elif ready == len(shots):
        note = ("Every shot is ready: planned, with an approved keyframe for every cut and a "
                "current video prompt.")  # fmt: skip
    else:
        note = (f"The keyframe pass is not complete: {ready} of {len(shots)} shots are ready "
                f"({framed} approved keyframes over {len(cuts)} planned cuts, {unplanned} "
                "shots not planned). Each shot's not_ready says what it lacks.")  # fmt: skip
    prompted = sum(1 for s in shots if s["prompt"])
    locked = sum(1 for s in shots if s["locked"])
    return {"shots": len(shots), "shots_ready": ready, "shots_with_prompt": prompted,
            "shots_locked": locked, "shots_planned": len(shots) - unplanned,
            "cuts_planned": len(cuts), "cuts_with_keyframe": framed,
            "keyframe_work_running": running, "note": note}  # fmt: skip


def export_prompt(vp: ShotVideoPrompt, files: dict[str, str]) -> dict[str, Any] | None:
    """A stored prompt in the export's form, each picture named by its file's name; None
    when it names a picture the export doesn't carry."""
    lost = False

    def name(m: Any) -> str:
        nonlocal lost
        f = files.get(m.group(1))
        if f is None:
            lost = True
            return m.group(0)
        return f"{{pic:{f.rsplit('/', 1)[-1]}}}"

    blocks = [{"sub_shot": b.sub_shot, "text": PIC.sub(name, b.text), "camera": b.camera}
              for b in vp.blocks]  # fmt: skip
    if lost:
        return None
    return {"format": PROMPT_FORMAT, "blocks": blocks, "sound": vp.sound, "notes": vp.notes,
            "advice": vp.advice, "model": vp.model,
            "written_at": vp.updated_at.isoformat()}  # fmt: skip
