"""Human-readable Markdown view of a production package."""

from __future__ import annotations

from .models import ProductionPackage


def _ts(ms: int) -> str:
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{(ms % 1000) // 100}"


def render_markdown(pkg: ProductionPackage) -> str:
    bible = pkg.story_bible
    chars = {c.character_id: c.name for c in bible.characters}
    out: list[str] = [
        f"# {bible.title}",
        "",
        f"**Logline:** {bible.logline}",
        f"package `{pkg.package_id}` · revision `{pkg.revision_id}`",
        "",
        "## Characters",
        *[f"- `{c.character_id}` {c.name}: {c.goal} ({c.motivation})" for c in bible.characters],
        "",
    ]
    out += _sheets(pkg)
    timing = {t.episode_id: t for t in pkg.timing}
    for ep in pkg.episodes:
        t = timing.get(ep.episode_id)
        out += [
            f"## Episode {ep.number}: {ep.title}",
            (
                f"planned_timing_valid={t.planned_timing_valid if t else None} · "
                f"spoken_timing_verified={t.spoken_timing_verified if t else None}"
            ),
            "",
        ]
        for sc in ep.scenes:
            out.append(
                f"### {sc.scene_id} {sc.int_ext}. {sc.location_id} - {sc.time_of_day} "
                f"({sc.target_duration_ms // 1000}s)"
            )
            for b in sc.beats:
                if b.action:
                    out.append(f"*{b.action}*")
                if b.dialogue:
                    vo = " (V.O.)" if b.dialogue.voice_only else ""
                    name = chars.get(b.dialogue.character_id, b.dialogue.character_id)
                    out.append(f"**{name}{vo}:** {b.dialogue.text}")
            out.append("")
        out += ["| shot | time | framing | action | assets |", "| --- | --- | --- | --- | --- |"]
        for sh in (s for s in pkg.shot_list if s.episode_id == ep.episode_id):
            out.append(
                f"| {sh.shot_id} | {_ts(sh.start_ms)}–{_ts(sh.end_ms)} | {sh.framing} | "
                f"{sh.visual_action} | {len(sh.asset_requirement_ids)} |"
            )
        out.append("")
    out += [
        "## Asset requirements",
        "| id | type | status | used by |",
        "| --- | --- | --- | --- |",
    ]
    for r in pkg.asset_requirements.items:
        out.append(
            f"| {r.requirement_id} | {r.asset_type} | {r.requirement_status} | {len(r.used_by)} shots |"
        )
    if pkg.qa_report.hard_issues:
        out += ["", "## Hard check issues"]
        out += [f"- [{i.check}] `{i.object_id}`: {i.evidence}" for i in pkg.qa_report.hard_issues]
    return "\n".join(out) + "\n"


def _fields(obj: object, names: list[str]) -> list[str]:
    lines = []
    for name in names:
        value = getattr(obj, name, "")
        if isinstance(value, list):
            value = "; ".join(value)
        if value:
            lines.append(f"  - {name.replace('_', ' ')}: {value}")
    return lines


def _sheets(pkg: ProductionPackage) -> list[str]:
    out: list[str] = []
    if pkg.cast_sheet:
        out += [f"## Cast sheet (schema {pkg.cast_sheet.schema_version})", ""]
        for c in pkg.cast_sheet.characters:
            seen = "" if c.on_screen else " (not seen on screen)"
            out.append(f"- `{c.character_id}` **{c.name}**{seen}")
            out += _fields(c, ["age_range", "gender_presentation", "build_and_height", "face",
                               "hair", "distinguishing_features", "default_wardrobe",
                               "wardrobe_palette", "posture_and_mannerisms", "voice",
                               "must_keep", "design_freedom"])  # fmt: skip
        for lk in pkg.cast_sheet.looks:
            out.append(f"- look `{lk.look_id}` ({lk.character_id})")
            out += _fields(lk, ["wardrobe", "hair_and_makeup", "condition",
                                "changes_from_base", "must_keep"])  # fmt: skip
        out.append("")
    if pkg.set_sheet:
        out += [f"## Set sheet (schema {pkg.set_sheet.schema_version})", ""]
        for loc in pkg.set_sheet.locations:
            out.append(f"- `{loc.location_id}` **{loc.name}** ({loc.interior_exterior})")
            out += _fields(loc, ["era_and_region", "layout", "key_furnishings",
                                 "materials_and_textures", "color_palette", "lighting",
                                 "entrances_and_blocking", "ambient_sound", "must_keep",
                                 "design_freedom"])  # fmt: skip
        for st in pkg.set_sheet.states:
            out.append(f"- state `{st.location_state_id}` ({st.location_id})")
            out += _fields(st, ["time_of_day", "lighting", "dressing_changes", "condition",
                                "changes_from_base", "must_keep"])  # fmt: skip
        out.append("")
    if pkg.prop_sheet:
        out += [f"## Prop sheet (schema {pkg.prop_sheet.schema_version})", ""]
        for p in pkg.prop_sheet.props:
            out.append(f"- `{p.prop_id}` **{p.name}**")
            out += _fields(p, ["size_and_scale", "materials", "color_and_finish",
                               "condition_and_wear", "distinguishing_marks", "handling",
                               "must_keep", "design_freedom"])  # fmt: skip
        for st in pkg.prop_sheet.states:
            out.append(f"- state `{st.prop_state_id}` ({st.prop_id})")
            out += _fields(st, ["condition", "changes_from_base", "must_keep"])
        out.append("")
    return out
