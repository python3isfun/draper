"""Production hand-off: one zip with everything a team needs to produce and shoot.

Layout (inside a folder named after the title and revision):

    00_README.md                  what's here, status, how to read it
    01_story/story_bible.md       logline, synopsis, characters, locations, props, outline
    02_script/episode_NN.md       screenplay-style script per episode (+ script.json)
    03_storyboard/shot_list.csv   one row per shot, for a spreadsheet (+ storyboard.md,
                                  dialogue_schedule.csv, storyboard.json)
    04_design/                    cast, set and prop sheets (.md + .json)
    05_assets/                    asset requirements (.csv + .json) and reference_images/
                                  (newest finished version per deliverable, + index.csv)
    06_qa/qa_report.md            hard checks and creative review
    07_sources/                   the job's idea text and uploaded photos/videos
    08_intimacy/intimacy_plan.md  scenes with intimacy, for the intimacy coordinator
                                  (only when the story has any)
    package.json                  the whole package, machine-readable

CSV files are UTF-8 with a BOM so spreadsheet apps open them correctly. Text is stored
compressed; images and videos are stored as is (already compressed).
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .images import design_slots, selected_image
from .media import MediaError, MediaStore
from .models import Job, ProductionPackage, SourceAsset
from .models.images import DesignOption, GeneratedImage
from .render import sheet_md, timestamp

MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}


def slug(text: str, fallback: str = "drama") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:60] or fallback


ts = timestamp


def secs(ms: int) -> str:
    return f"{ms / 1000:g}"


class Names:
    """Display names for IDs, so the hand-off reads without the ID tables."""

    def __init__(self, pkg: ProductionPackage) -> None:
        b = pkg.story_bible
        self.char = {c.character_id: c.name for c in b.characters}
        self.loc = {loc.location_id: loc.name for loc in b.locations}
        self.prop = {p.prop_id: p.name for p in b.props}
        self.look = {lk.look_id: lk.description for lk in b.looks}
        self.loc_state = {s.location_state_id: s.description for s in b.location_states}
        self.prop_state = {s.prop_state_id: s.description for s in b.prop_states}
        self.lines = {
            b.dialogue.dialogue_id: b.dialogue
            for ep in pkg.episodes
            for sc in ep.scenes
            for b in sc.beats
            if b.dialogue
        }

    def any(self, entity_id: str) -> str:
        for table in (self.char, self.loc, self.prop, self.look, self.loc_state, self.prop_state):
            if entity_id in table:
                return table[entity_id]
        return entity_id


def _csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _json(obj: Any) -> bytes:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    elif isinstance(obj, list):
        obj = [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in obj]
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Documents


def story_bible_md(pkg: ProductionPackage, n: Names) -> str:
    b = pkg.story_bible
    out = [f"# {b.title}", "", f"**Logline:** {b.logline}", ""]
    out += [
        f"- **Genre:** {b.genre}",
        f"- **Audience:** {b.audience_assumption}",
        f"- **Emotional goal:** {b.emotional_goal}",
        f"- **Core conflict:** {b.core_conflict}",
        "",
        "## Series synopsis",
        "",
        b.series_synopsis,
        "",
    ]
    if b.future_direction:
        out += ["## Where the series goes next", "", b.future_direction, ""]
    out += ["## Characters", ""]
    for c in b.characters:
        out.append(f"### {c.name}" + (f" ({c.role})" if c.role else ""))
        out += [f"- **Goal:** {c.goal}", f"- **Motivation:** {c.motivation}"]
        if b.character_arcs.get(c.character_id):
            out.append(f"- **Arc:** {b.character_arcs[c.character_id]}")
        for other, rel in c.relationships.items():
            out.append(f"- **{n.char.get(other, other)}:** {rel}")
        if c.knowledge:
            out.append(f"- **Knows:** {'; '.join(c.knowledge)}")
        looks = [lk.description for lk in b.looks if lk.character_id == c.character_id]
        if looks:
            out.append(f"- **Looks:** {'; '.join(looks)}")
        out.append("")
    out += ["## Locations", ""]
    for loc in b.locations:
        states = [s.description for s in b.location_states if s.location_id == loc.location_id]
        out.append(f"- **{loc.name}**" + (f": {loc.summary}" if loc.summary else ""))
        if states:
            out.append(f"  - States: {'; '.join(states)}")
    out.append("")
    if b.props:
        out += ["## Key props", ""]
        for p in b.props:
            owner = n.char.get(p.owner_character_id or "", "")
            states = [s.description for s in b.prop_states if s.prop_id == p.prop_id]
            out.append(
                f"- **{p.name}**"
                + (f": {p.story_function}" if p.story_function else "")
                + (f" (belongs to {owner})" if owner else "")
            )
            if states:
                out.append(f"  - States: {'; '.join(states)}")
        out.append("")
    if b.beat_outlines:
        out += ["## Episode outline", ""]
        for i, bo in enumerate(b.beat_outlines, 1):
            out += [
                f"### Episode {i}",
                f"- **Opening:** {bo.opening}",
                f"- **Protagonist acts:** {bo.protagonist_action}",
                f"- **Cost or reversal:** {bo.cost_or_reversal}",
                f"- **Ending change:** {bo.ending_change}",
                "",
            ]
    if pkg.assumptions or pkg.open_questions:
        out += ["## Assumptions and open questions", ""]
        out += [f"- Assumption: {a}" for a in pkg.assumptions]
        out += [f"- Open question: {q}" for q in pkg.open_questions]
        out.append("")
    return "\n".join(out)


def episode_md(pkg: ProductionPackage, ep_index: int, n: Names) -> str:
    """Screenplay-style script: scene headings, action, CHARACTER cues and dialogue."""
    ep = pkg.episodes[ep_index]
    out = [
        f"# Episode {ep.number}: {ep.title}",
        "",
        f"*Opening:* {ep.opening_event}  ",
        f"*Main conflict:* {ep.main_conflict}  ",
        f"*Ending:* {ep.ending_event}",
        "",
    ]
    for sc in ep.scenes:
        place = n.loc.get(sc.location_id, sc.location_id).upper()
        out.append(f"## {sc.number}. {sc.int_ext}. {place} - {sc.time_of_day.upper()}")
        state = n.loc_state.get(sc.location_state_id)
        details = [f"target {secs(sc.target_duration_ms)}s"]
        if state:
            details.insert(0, state)
        cast = ", ".join(n.char.get(c, c) for c in sc.character_ids)
        out += [f"*{' · '.join(details)} · {cast}*", ""]
        for beat in sc.beats:
            if beat.action:
                out += [beat.action, ""]
            if beat.dialogue:
                d = beat.dialogue
                cue = n.char.get(d.character_id, d.character_id).upper()
                if d.voice_only:
                    cue += " (V.O.)"
                out.append(f"**{cue}**  ")
                if d.delivery:
                    out.append(f"({d.delivery})  ")
                out += [d.text, ""]
            if beat.sound:
                out += [f"*Sound: {beat.sound}*", ""]
        if sc.new_information:
            out += [f"> New information: {'; '.join(sc.new_information)}", ""]
        if sc.intimacy_level != "none":
            out += [f"> Intimacy: {sc.intimacy_level} (see 08_intimacy/intimacy_plan.md)", ""]
    return "\n".join(out)


INTIMACY_LABEL = {
    "kiss": "Kiss or embrace",
    "implied": "Implied (cuts away)",
    "simulated": "Simulated intimacy on screen",
}


def intimacy_scenes(pkg: ProductionPackage) -> list[tuple[Any, Any]]:
    return [(ep, sc) for ep in pkg.episodes for sc in ep.scenes if sc.intimacy_level != "none"]


def intimacy_md(pkg: ProductionPackage, n: Names, rating: str) -> str:
    """The intimacy plan: every scene with intimacy, staged for the coordinator and the
    director. Text only: the package never contains intimate images."""
    out = [
        f"# Intimacy plan: {pkg.story_bible.title}",
        "",
        f"Content rating: **{rating}**.",
        "",
        "Before shooting any scene below:",
        "",
        (
            "- Hire an intimacy coordinator for implied and simulated scenes (SAG-AFTRA expects "
            "one for nudity and simulated sex)."
        ),
        (
            "- Agree each scene's choreography, modesty garments and barriers with the actors in "
            "advance; get nudity riders signed where nudity is listed."
        ),
        "- Actors are adults (18+). Consent can be withdrawn at any time, including on the day.",
        (
            "- Closed set means only essential crew; monitors are restricted, and no stills or "
            "behind-the-scenes footage are taken without the actors' written consent."
        ),
        "",
    ]
    for ep, sc in intimacy_scenes(pkg):
        it = sc.intimacy
        place = n.loc.get(sc.location_id, sc.location_id)
        who = ", ".join(n.char.get(c, c) for c in (it.character_ids or sc.character_ids))
        out += [
            f"## Episode {ep.number}, scene {sc.number}: {place}",
            "",
            f"- Level: {INTIMACY_LABEL.get(it.level, it.level)}",
            f"- Characters: {who}",
            f"- On screen: {it.on_screen or '(not described)'}",
            f"- Implied, not shown: {it.off_screen or '(nothing)'}",
            f"- Nudity: {it.nudity}",
            f"- Closed set: {'yes' if it.closed_set else 'no'}",
            "- Consent beats: " + ("; ".join(it.consent_beats) or "(none listed)"),
            f"- Coordinator needed: {'yes' if it.level in ('implied', 'simulated') else 'advised'}",
            "",
        ]
    return "\n".join(out)


SHOT_COLUMNS = [
    "episode", "scene", "shot", "start", "end", "duration_s", "framing", "camera",
    "on_screen", "looks", "location", "props", "visual_action", "dialogue", "sound",
    "continuity", "narrative_function", "asset_requirements", "needs_shot_reference",
    "generation_notes", "shot_id",
]  # fmt: skip


def shot_rows(pkg: ProductionPackage, n: Names) -> list[dict[str, Any]]:
    ep_no = {ep.episode_id: ep.number for ep in pkg.episodes}
    sc_no = {sc.scene_id: sc.number for ep in pkg.episodes for sc in ep.scenes}
    per_scene: dict[str, int] = {}
    rows = []
    for sh in pkg.shot_list:
        per_scene[sh.scene_id] = per_scene.get(sh.scene_id, 0) + 1
        dialogue = []
        for cue in sh.dialogue_cues:
            line = n.lines.get(cue.dialogue_id)
            if line:
                who = n.char.get(line.character_id, line.character_id)
                vo = " (V.O.)" if line.voice_only else ""
                dialogue.append(f'{who}{vo}: "{line.text}" [{ts(cue.start_ms)}-{ts(cue.end_ms)}]')
        rows.append(
            {
                "episode": ep_no.get(sh.episode_id, sh.episode_id),
                "scene": sc_no.get(sh.scene_id, sh.scene_id),
                "shot": per_scene[sh.scene_id],
                "start": ts(sh.start_ms),
                "end": ts(sh.end_ms),
                "duration_s": secs(sh.duration_ms),
                "framing": sh.framing,
                "camera": sh.camera,
                "on_screen": ", ".join(n.char.get(c, c) for c in sh.character_ids),
                "looks": "; ".join(
                    f"{n.char.get(r.character_id, r.character_id)}: {n.look.get(r.look_id, r.look_id)}"
                    for r in sh.character_look_refs
                ),
                "location": f"{n.loc.get(sh.location_id, sh.location_id)}"
                f" ({n.loc_state.get(sh.location_state_id, sh.location_state_id)})",
                "props": "; ".join(
                    f"{n.prop.get(r.prop_id, r.prop_id)} ({n.prop_state.get(r.prop_state_id, r.prop_state_id)})"
                    for r in sh.prop_state_refs
                ),
                "visual_action": sh.visual_action,
                "dialogue": " | ".join(dialogue),
                "sound": sh.sound,
                "continuity": sh.continuity,
                "narrative_function": sh.narrative_function,
                "asset_requirements": ", ".join(sh.asset_requirement_ids),
                "needs_shot_reference": "yes" if sh.needs_shot_reference else "",
                "generation_notes": sh.generation_notes,
                "shot_id": sh.shot_id,
            }
        )
    return rows


def storyboard_md(pkg: ProductionPackage, rows: list[dict[str, Any]]) -> str:
    out = ["# Storyboard", ""]
    timing = {t.episode_id: t for t in pkg.timing}
    for ep in pkg.episodes:
        t = timing.get(ep.episode_id)
        out += [f"## Episode {ep.number}: {ep.title}", ""]
        if t:
            out += [
                f"Timeline valid: {'yes' if t.planned_timing_valid else 'no'} · spoken timing "
                f"verified: {'yes' if t.spoken_timing_verified else 'no (needs a read-through)'}"
                + (f" · {t.speech_rate_assumption}" if t.speech_rate_assumption else ""),
                "",
            ]
        for r in (r for r in rows if r["episode"] == ep.number):
            out.append(
                f"### {r['scene']}.{r['shot']} · {r['start']}-{r['end']} ({r['duration_s']}s) · "
                f"{r['framing']}"
            )
            out += [f"- **Camera:** {r['camera']}", f"- **Action:** {r['visual_action']}"]
            for key, label in (
                ("on_screen", "On screen"), ("location", "Location"), ("props", "Props"),
                ("dialogue", "Dialogue"), ("sound", "Sound"), ("continuity", "Continuity"),
                ("narrative_function", "Purpose"),
            ):  # fmt: skip
                if r[key]:
                    out.append(f"- **{label}:** {r[key]}")
            out.append("")
    return "\n".join(out)


def dialogue_rows(pkg: ProductionPackage, n: Names) -> list[dict[str, Any]]:
    ep_no = {ep.episode_id: ep.number for ep in pkg.episodes}
    rows = []
    for t in pkg.timing:
        for e in t.dialogue_schedule:
            line = n.lines.get(e.dialogue_id)
            rows.append(
                {
                    "episode": ep_no.get(t.episode_id, t.episode_id),
                    "start": ts(e.start_ms),
                    "end": ts(e.end_ms),
                    "character": n.char.get(line.character_id, line.character_id) if line else "",
                    "voice_only": "yes" if line and line.voice_only else "",
                    "line": line.text if line else "",
                    "delivery": line.delivery if line else "",
                    "dialogue_id": e.dialogue_id,
                }
            )
    return rows


ASSET_COLUMNS = [
    "requirement_id", "asset_type", "subject", "priority", "status", "design_brief",
    "must_keep", "design_freedom", "deliverables", "used_in_shots", "depends_on",
    "reference_images",
]  # fmt: skip


def image_name(img: GeneratedImage, take: int | None = None) -> str:
    """Unique per image: the deliverable's own suffix (face, full, wide, ...), then the
    take and draw for an image drawn from a take (`_full` for a full-size render of a
    selected draft), else the version."""
    ext = Path(img.file or "").suffix or ".jpg"
    key = img.deliverable_id.removeprefix(f"d_{img.requirement_id}_")
    if img.option_id:
        tail = f"take{take or 0}_draw{img.draw}" + ("_full" if img.finalizes else "")
    else:
        tail = f"v{img.version}"
    return f"{img.requirement_id}__{slug(key, 'image')}_{tail}{ext}"


def qa_md(pkg: ProductionPackage) -> str:
    qa = pkg.qa_report
    out = [
        "# QA report",
        "",
        f"Hard checks: {'all passed' if not qa.hard_issues else f'{len(qa.hard_issues)} issues'}",
        "",
    ]
    if qa.hard_issues:
        out += ["## Hard check issues", ""]
        out += [f"- [{i.check}] {i.object_id}: {i.evidence}" for i in qa.hard_issues]
        out.append("")
    out += ["## Creative review", ""]
    if qa.creative_issues:
        out += [
            f"- **{i.severity}** {i.object_id}: {i.evidence}"
            + (f" (fix: {i.fix_scope})" if i.fix_scope else "")
            for i in qa.creative_issues
        ]
    else:
        out.append("No creative issues reported.")
    ep_no = {ep.episode_id: ep.number for ep in pkg.episodes}
    unverified = [
        ep_no.get(t.episode_id, t.episode_id) for t in pkg.timing if not t.spoken_timing_verified
    ]
    if unverified:
        which = (
            "any episode"
            if len(unverified) == len(pkg.timing)
            else "episode " + ", ".join(str(e) for e in unverified)
        )
        out += [
            "",
            (
                f"Note: spoken timing is not verified for {which} yet; the timeline uses a "
                "text-stage speech-rate estimate. Do a read-through before locking the edit."
            ),
        ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# The zip


def build_export(
    pkg: ProductionPackage,
    dest: Path,
    *,
    job: Job | None,
    images: list[GeneratedImage],
    assets: dict[str, SourceAsset],
    media: MediaStore | None,
    options: list[DesignOption] | None = None,
) -> str:
    """Write the hand-off zip to `dest`; returns the download file name."""
    takes = {o.option_id: o for o in options or []}

    def take_of(img: GeneratedImage) -> DesignOption | None:
        return takes.get(img.option_id or "")

    def edited_from(opt: DesignOption | None) -> str:
        src = takes.get(opt.edited_from or "") if opt else None
        return f"take {src.index}" if src else ""

    def name_of(img: GeneratedImage) -> str:
        opt = take_of(img)
        return image_name(img, opt.index if opt else None)

    n = Names(pkg)
    root = f"{slug(pkg.story_bible.title)}-{pkg.revision_id}"
    rows = shot_rows(pkg, n)
    deliverable_view = {
        (r.requirement_id, d.deliverable_id): d.view
        for r in pkg.asset_requirements.items
        for d in r.deliverables
    }

    def safe_path(rel: str | None) -> Path | None:
        """A stored file to include, or None if it's missing or outside the media store
        (that file is skipped; the export still succeeds)."""
        if media is None or not rel:
            return None
        try:
            path = media.path(rel)
        except MediaError:
            return None
        return path if path.is_file() else None

    # The chosen image per deliverable: the selected one, else the newest finished one.
    keys = sorted({(i.requirement_id, i.deliverable_id) for i in images})
    done = [img for k in keys if (img := selected_image(images, k)) and img.file]
    chosen_ids = {i.image_id for i in done}
    # Only images whose file is really there: everything that lists them uses this.
    added: list[tuple[GeneratedImage, str, str, Path]] = []
    for img in done:
        if (src := safe_path(img.file)) is not None:
            view = deliverable_view.get((img.requirement_id, img.deliverable_id), "image")
            added.append((img, view, name_of(img), src))
    # Every other finished image drawn from a take: the options the team can revisit.
    alternatives = [
        (img, src)
        for img in sorted(images, key=lambda i: (i.requirement_id, i.deliverable_id,
                                                 take_of(i).index if take_of(i) else 0, i.draw))
        if img.option_id and img.status == "done" and img.image_id not in chosen_ids
        and (src := safe_path(img.file)) is not None
    ]  # fmt: skip
    files_by_req: dict[str, list[str]] = {}
    for img, _, name, _ in added:
        files_by_req.setdefault(img.requirement_id, []).append(name)

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:

        def text(path: str, content: str | bytes) -> None:
            data = content.encode("utf-8") if isinstance(content, str) else content
            z.writestr(f"{root}/{path}", data)

        def media_file(path: str, src: Path) -> None:
            z.write(src, f"{root}/{path}", compress_type=zipfile.ZIP_STORED)

        # Story and script
        text("01_story/story_bible.md", story_bible_md(pkg, n))
        text("01_story/story_bible.json", _json(pkg.story_bible))
        for i, ep in enumerate(pkg.episodes):
            text(f"02_script/episode_{ep.number:02d}.md", episode_md(pkg, i, n))
        text("02_script/script.json", _json(pkg.episodes))

        # Storyboard
        text("03_storyboard/shot_list.csv", _csv(rows, SHOT_COLUMNS))
        text("03_storyboard/storyboard.md", storyboard_md(pkg, rows))
        text(
            "03_storyboard/dialogue_schedule.csv",
            _csv(
                dialogue_rows(pkg, n),
                [
                    "episode",
                    "start",
                    "end",
                    "character",
                    "voice_only",
                    "line",
                    "delivery",
                    "dialogue_id",
                ],
            ),
        )
        text(
            "03_storyboard/storyboard.json",
            _json(
                {
                    "shots": [s.model_dump(mode="json") for s in pkg.shot_list],
                    "timing": [t.model_dump(mode="json") for t in pkg.timing],
                }
            ),
        )

        # Design sheets
        for which, attr, name in (
            ("cast", "cast_sheet", "cast_sheet"),
            ("sets", "set_sheet", "set_sheet"),
            ("props", "prop_sheet", "prop_sheet"),
        ):
            lines = sheet_md(pkg, which, level=1)
            if lines:
                text(f"04_design/{name}.md", "\n".join(lines))
                text(f"04_design/{name}.json", _json(getattr(pkg, attr)))

        # Assets and reference images
        ar = pkg.asset_requirements
        asset_rows = [
            {
                "requirement_id": r.requirement_id,
                "asset_type": r.asset_type,
                "subject": "; ".join(
                    n.any(s.entity_id) + (f" / {n.any(s.variant_id)}" if s.variant_id else "")
                    for s in r.subject_refs
                ),
                "priority": r.priority,
                "status": r.requirement_status,
                "design_brief": r.design_brief,
                "must_keep": "; ".join(r.must_keep),
                "design_freedom": "; ".join(r.design_freedom),
                "deliverables": "; ".join(f"{d.view} ({d.purpose})" for d in r.deliverables),
                "used_in_shots": ", ".join(u.shot_id for u in r.used_by),
                "depends_on": ", ".join(r.depends_on),
                "reference_images": ", ".join(files_by_req.get(r.requirement_id, [])),
            }
            for r in ar.items
        ]
        text("05_assets/asset_requirements.csv", _csv(asset_rows, ASSET_COLUMNS))
        text("05_assets/asset_requirements.json", _json(ar))
        index_rows = []
        for img, view, name, src in added:
            media_file(f"05_assets/reference_images/{name}", src)
            index_rows.append(
                {
                    "file": name,
                    "requirement_id": img.requirement_id,
                    "view": view,
                    "version": img.version,
                    "size": img.size,
                    "seed": img.seed,
                    "model": img.model,
                    "prompt": img.prompt,
                }
            )
        if index_rows:
            text(
                "05_assets/reference_images/index.csv",
                _csv(
                    index_rows,
                    [
                        "file",
                        "requirement_id",
                        "view",
                        "version",
                        "size",
                        "seed",
                        "model",
                        "prompt",
                    ],
                ),
            )

        # Design takes: the chosen take per deliverable, and every other option
        slot_names = {
            (s["requirement_id"], s["deliverable_id"]): (s["entity"], s["view"], s["variant"])
            for s in design_slots(pkg)
        }
        if takes:
            lines = [
                "# Selected designs",
                "",
                (
                    "The take chosen for each image, with what makes it distinct. Other takes "
                    "are in `05_assets/options/`."
                ),
                "",
            ]
            for img, view, name, _ in added:
                opt = take_of(img)
                if opt is None:
                    continue
                entity, _, variant = slot_names.get(
                    (img.requirement_id, img.deliverable_id), ("", view, "")
                )
                title = entity + (f" ({variant})" if variant else "") + f", {view}"
                picked = "selected" if img.selected else "newest (nothing selected)"
                lines += [
                    f"## {title}",
                    f"- Take {opt.index}: **{opt.label}** ({picked})",
                    *([f"- {opt.profile}"] if opt.profile else []),
                    *(
                        [
                            f"- Prompt edited by hand from {edited_from(opt)}:",
                            "",
                            "  ```text",
                            *(f"  {line}" for line in opt.prompt.splitlines()),
                            "  ```",
                        ]
                        if opt.status == "edited"
                        else []
                    ),
                    f"- Image: `05_assets/reference_images/{name}`",
                    "",
                ]
            text("04_design/selected_designs.md", "\n".join(lines))
        option_rows = []
        for img, src in alternatives:
            opt = take_of(img)
            name = name_of(img)
            media_file(f"05_assets/options/{name}", src)
            entity, view, variant = slot_names.get(
                (img.requirement_id, img.deliverable_id), ("", "", "")
            )
            option_rows.append(
                {
                    "file": name,
                    "entity": entity,
                    "variant": variant,
                    "view": view,
                    "take": opt.index if opt else "",
                    "label": opt.label if opt else "",
                    "profile": opt.profile if opt else "",
                    "edited_from": edited_from(opt),
                    "draw": img.draw,
                    "size": img.size,
                    "seed": img.seed,
                    "prompt": img.prompt,
                }
            )
        if option_rows:
            text(
                "05_assets/options/options.csv",
                _csv(
                    option_rows,
                    [
                        "file",
                        "entity",
                        "variant",
                        "view",
                        "take",
                        "label",
                        "profile",
                        "edited_from",
                        "draw",
                        "size",
                        "seed",
                        "prompt",
                    ],
                ),
            )

        # QA
        text("06_qa/qa_report.md", qa_md(pkg))

        # Sources
        src_lines = ["# Sources", ""]
        if job is not None:
            for i, src in enumerate(job.request.sources, 1):
                label = f"{i}. {src.type}" + (
                    f" ({src.use_as.replace('_', ' ')})" if src.use_as else ""
                )
                src_lines.append(f"## {label}")
                asset = assets.get(src.asset_id or "")
                if asset is not None:
                    path = safe_path(asset.file)
                    if path is not None and path.suffix.lower() in MEDIA_SUFFIXES:
                        name = f"{i:02d}_{asset.asset_id}{path.suffix.lower()}"
                        media_file(f"07_sources/{name}", path)
                        src_lines.append(f"File: `{name}` (uploaded as {asset.filename})")
                if src.text:
                    prefix = "Prompt" if src.type in ("image", "video") else "Text"
                    src_lines.append(f"{prefix}: {src.text}")
                src_lines.append("")
        else:
            src_lines.append("The job that made this package was not found.")
        text("07_sources/sources.md", "\n".join(src_lines))

        # Intimacy plan (text only), when any scene has intimacy
        if intimacy_scenes(pkg):
            rating = job.request.options.content_rating if job else "teen"
            text("08_intimacy/intimacy_plan.md", intimacy_md(pkg, n, rating))

        # Everything, and the README
        text("package.json", _json(pkg))
        text("00_README.md", readme_md(pkg, job, rows, len(index_rows)))
    return f"{root}.zip"


def readme_md(pkg: ProductionPackage, job: Job | None, rows: list, n_images: int) -> str:
    b = pkg.story_bible
    passed = not pkg.qa_report.hard_issues
    eps = len(pkg.episodes)
    return "\n".join(
        [
            f"# {b.title}",
            "",
            b.logline,
            "",
            f"- Package `{pkg.package_id}`, revision `{pkg.revision_id}`"
            + (f", job `{job.job_id}` ({job.status})" if job else ""),
            (
                f"- {eps} episodes · {len(rows)} shots · {len(pkg.asset_requirements.items)} asset "
                f"requirements · {n_images} reference images"
            ),
            f"- Hard checks: {'all passed' if passed else 'NOT all passed (see 06_qa)'}",
            "",
            "## What's here",
            "",
            "| Folder | For | Contents |",
            "| --- | --- | --- |",
            (
                "| `01_story/` | everyone | Story bible: logline, synopsis, characters and their "
                "goals, locations, key props, episode outline |"
            ),
            (
                "| `02_script/` | director, cast | One screenplay-style script per episode: scene "
                "headings, action, dialogue |"
            ),
            (
                "| `03_storyboard/` | director, DP, editor | `shot_list.csv` (one row per shot: "
                "timing, framing, camera, who and what is on screen, dialogue, sound, continuity), "
                "`storyboard.md` (the same, readable), `dialogue_schedule.csv` (every line with "
                "its start and end time) |"
            ),
            (
                "| `04_design/` | casting, wardrobe, production design, props | Cast, set and prop "
                "sheets: how each character, location and prop looks, what must stay the same, "
                "what is free to design |"
            ),
            (
                "| `05_assets/` | art department | Asset requirements (design brief, must keep, "
                "deliverables, which shots use each), the chosen reference images, and every "
                "other design option in `options/` |"
            ),
            "| `06_qa/` | producer | Hard-check results and the creative review |",
            "| `07_sources/` | everyone | The original idea and any uploaded photos or videos |",
            *(
                [
                    (
                        "| `08_intimacy/` | director, intimacy coordinator | Every scene with "
                        "intimacy: level, what is shown and implied, nudity, closed set, consent "
                        "beats |"
                    )
                ]
                if intimacy_scenes(pkg)
                else []
            ),
            "| `package.json` | tooling | The whole package, machine-readable |",
            "",
            "## Notes",
            "",
            (
                "- Each episode runs 120 s; shot times in `shot_list.csv` are positions in the "
                "finished episode (mm:ss.s)."
            ),
            (
                "- Spoken timing comes from a text-stage estimate, not a measured read-through: "
                "time the dialogue before locking the edit."
            ),
            (
                "- Reference images are design references for wardrobe, sets and props, not "
                "final frames. `05_assets/reference_images/index.csv` lists the prompt and seed of "
                "each, so any of them can be regenerated."
            ),
            "",
        ]
    )
