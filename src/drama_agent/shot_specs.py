"""Shot specs for video production: per shot, everything dramer makes the clip from.

`03_storyboard/shot_specs.json` (spec_version 2) is the hand-off contract. Each shot's
entry is complete: dramer stores `spec` as the shot's spec, sends `references` (in order,
up to its picture limit) with the shot, files `cast` as the shot's members, and writes the
video prompt from them. Draper decides all of it, because it holds what decides it: the
cut plan, the approved keyframes, the scene reading, and the pictures each keyframe was
drawn from.

The references are in priority order, so a consumer with a smaller limit drops from the
end. A shot whose every cut has an approved keyframe is keyframe-first: the keyframes
show the people, the set and the props as each cut is, so after them come the faces (to
hold each likeness), the props (seen closer), and only then each person's design picture
and the set. Any other shot sends each person's picture, the set and the props first, then
the faces. The keyframes are references, not start frames: the video model matches each
cut to its keyframe.

All file paths are relative to the export's root folder.
"""

from __future__ import annotations

from typing import Any

from .models import ProductionPackage
from .models.images import GeneratedImage
from .shot_frames import (
    MAX_REFERENCES,
    FrameRef,
    ShotPlan,
    chosen_pictures,
    plan_of,
    plan_problems,
    shot_lines,
    shot_picks,
    sub_shot_lines,
)
from .shot_reading import PersonState, SceneReading

SHOT_SPECS_VERSION = 2
SHOT_SPECS_PATH = "03_storyboard/shot_specs.json"
IMAGES = "05_assets/reference_images"
KEYFRAMES = "05_assets/shot_keyframes"


def state_note(st: PersonState) -> str:
    """A person's state as one labelled line ("Wearing: a robe. Holding: one shoe."); a
    blank field is left out, so nothing is said about hands the reading left blank."""
    parts = [("Wearing", st.wearing), ("Hair", st.hair), ("Holding", st.carrying),
             ("", st.condition)]  # fmt: skip
    out = []
    for label, v in parts:
        v = (v or "").strip().rstrip(".; ")
        if v:
            out.append(f"{label}: {v}." if label else f"{v}.")
    return " ".join(out)


def shot_specs(
    pkg: ProductionPackage,
    stored: dict[str, ShotPlan],
    keyframes: dict[tuple[str, int], str],
    names: Any,
    readings: dict[str, SceneReading | None],
    images: list[GeneratedImage],
    exported: dict[str, str],
) -> dict[str, Any]:
    """The shot specs of every shot in shot-list order. `keyframes` maps (shot, sub-shot)
    to its approved keyframe's file in shot_keyframes/; `readings` each scene's fresh
    reading (or None); `exported` each exported design image to its file in
    reference_images/."""
    bible = pkg.story_bible
    looks = {x.look_id: x for x in bible.looks}
    loc_states = {x.location_state_id: x for x in bible.location_states}
    prop_states = {x.prop_state_id: x for x in bible.prop_states}
    set_states = {x.location_state_id: x for x in (pkg.set_sheet.states if pkg.set_sheet else [])}
    script = {b.dialogue.dialogue_id: b.dialogue for ep in pkg.episodes for sc in ep.scenes
              for b in sc.beats if b.dialogue}  # fmt: skip
    episode_no = {ep.episode_id: ep.number for ep in pkg.episodes}

    def image_ref(r: FrameRef, kind: str, cuts: list[int] | None = None) -> dict[str, Any] | None:
        name = exported.get(r.ref_id)
        if name is None:
            return None
        return {"file": f"{IMAGES}/{name}", "kind": kind, "entity_id": r.entity_id,
                "name": names.any(r.entity_id) if r.entity_id else "", "sub_shots": cuts or [],
                "likeness": r.likeness, "about": r.role}  # fmt: skip

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
            problems = plan_problems(pkg, shot, plan)
            frames = {sub.index: keyframes.get((shot.shot_id, sub.index)) for sub in plan.sub_shots}
            # A plan nobody made (one cut, the storyboard's) is kept only with a keyframe.
            kept = plan.source != "single" or any(frames.values())
            all_frames = kept and all(frames.values())
            reading = readings.get(shot.scene_id)
            chosen = {sub.index: chosen_pictures(pkg, shot, sub, reading) for sub in plan.sub_shots}

            # The keyframes (each cut's own file), in cut order.
            refs: list[dict[str, Any]] = [
                {"file": f"{KEYFRAMES}/{f}", "kind": "keyframe", "entity_id": None, "name": "",
                 "sub_shots": [i], "likeness": False, "about": f"approved keyframe of [Shot {i}]"}
                for i, f in frames.items() if f and kept
            ]  # fmt: skip

            # Each person's pictures: those the reading chose for their cuts (the first in
            # their look's place), else the storyboard's look.
            picks = shot_picks(pkg, images, shot)
            firsts: list[dict[str, Any]] = []
            later: list[dict[str, Any]] = []
            person_pics: set[str] = set()
            notes: dict[str, str] = {}
            for who in shot.character_ids:
                mine: dict[str, tuple[FrameRef, list[int]]] = {}
                for i, by_who in chosen.items():
                    if (r := by_who.get(who)) is not None:
                        mine.setdefault(r.ref_id, (r, []))[1].append(i)
                        if who not in notes and r.state is not None:
                            notes[who] = state_note(r.state)
                pics = [image_ref(r, "chosen", cuts) for r, cuts in mine.values()]
                pics = [p for p in pics if p is not None]
                if not pics:
                    look = next((r for r in picks.looks if r.entity_id == who), None)
                    pics = [p] if look and (p := image_ref(look, look.kind)) else []
                if pics:
                    firsts.append(pics[0])
                    later.extend(pics[1:])
                    person_pics.update(p["file"] for p in pics)
            faces = [p for r in picks.faces if (p := image_ref(r, "face"))
                     and p["file"] not in person_pics]  # fmt: skip
            sets = [p for r in picks.sets if (p := image_ref(r, "set"))]
            props = [p for r in picks.props if (p := image_ref(r, "prop"))]
            if all_frames:
                rest = [*faces, *props, *firsts, *later, *sets]
            else:
                rest = [*firsts, *sets, *props, *later, *faces]
            seen = {r["file"] for r in refs}
            for r in rest:
                if r["file"] not in seen:
                    seen.add(r["file"])
                    refs.append(r)

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
            st = set_states.get(shot.location_state_id)
            out.append({
                "shot_id": shot.shot_id,
                "episode": episode_no.get(shot.episode_id),
                "scene_id": shot.scene_id,
                "ready": not not_ready,
                "not_ready": not_ready,
                "keyframe_first": all_frames,
                "planned_by": plan.source,
                "spec": {
                    "summary": shot.narrative_function,
                    "action": shot.visual_action,
                    "camera": {"shot_size": shot.framing, "angle": "", "movement": shot.camera},
                    "lighting": st.lighting if st else "",
                    "target_duration_ms": plan.sub_shots[-1].end_ms if kept else shot.duration_ms,
                    "dialogue": dialogue,
                    # Lines spoken in the shot before, running over the cut into this one.
                    "carried": carried,
                    "sound_notes": shot.sound,
                    "continuity_notes": continuity,
                    "sub_shots": sub_shots if kept else None,
                },
                "references": refs,
                "cast": cast,
            })  # fmt: skip
    order = {s.shot_id: i for i, s in enumerate(pkg.shot_list)}
    out.sort(key=lambda s: order.get(s["shot_id"], len(order)))
    return {"spec_version": SHOT_SPECS_VERSION, "max_references": MAX_REFERENCES, "shots": out}
