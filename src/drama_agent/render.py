"""Human-readable Markdown view of a production package."""

from __future__ import annotations

from .models import ProductionPackage


def timestamp(ms: int) -> str:
    """mm:ss.s, truncated (never rounded up into an invalid "00:60.0")."""
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{(ms % 1000) // 100}"


_ts = timestamp


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


def _fields(obj: object, names: list[str], indent: str = "") -> list[str]:
    lines = []
    for name in names:
        value = getattr(obj, name, "")
        if isinstance(value, list):
            value = "; ".join(value)
        if value:
            lines.append(f"{indent}- {name.replace('_', ' ')}: {value}")
    return lines


CAST_FIELDS = ["age_range", "gender_presentation", "build_and_height", "face", "hair",
               "distinguishing_features", "default_wardrobe", "wardrobe_palette",
               "posture_and_mannerisms", "voice", "must_keep", "design_freedom"]  # fmt: skip
LOOK_FIELDS = ["wardrobe", "hair_and_makeup", "condition", "changes_from_base", "must_keep"]
SET_FIELDS = ["era_and_region", "layout", "key_furnishings", "materials_and_textures",
              "color_palette", "lighting", "entrances_and_blocking", "ambient_sound",
              "must_keep", "design_freedom"]  # fmt: skip
SET_STATE_FIELDS = ["time_of_day", "lighting", "dressing_changes", "condition",
                    "changes_from_base", "must_keep"]  # fmt: skip
PROP_FIELDS = ["size_and_scale", "materials", "color_and_finish", "condition_and_wear",
               "distinguishing_marks", "handling", "must_keep", "design_freedom"]  # fmt: skip
PROP_STATE_FIELDS = ["condition", "changes_from_base", "must_keep"]


def sheet_md(pkg: ProductionPackage, which: str, level: int = 2) -> list[str]:
    """One design sheet as Markdown lines: the sheet title at `level`, each entity one level
    below, its looks/states listed under it. Empty if the package has no such sheet. Used
    by the Markdown view and the production zip."""
    h1, h2 = "#" * level, "#" * (level + 1)
    out: list[str] = []
    if which == "cast" and pkg.cast_sheet:
        s = pkg.cast_sheet
        out += [f"{h1} Cast sheet (schema {s.schema_version})", ""]
        for c in s.characters:
            out.append(f"{h2} {c.name}" + ("" if c.on_screen else " (not seen on screen)"))
            out += _fields(c, CAST_FIELDS)
            for lk in (x for x in s.looks if x.character_id == c.character_id):
                out.append(f"- look `{lk.look_id}`:")
                out += _fields(lk, LOOK_FIELDS, "  ")
            out.append("")
    elif which == "sets" and pkg.set_sheet:
        s = pkg.set_sheet
        out += [f"{h1} Set sheet (schema {s.schema_version})", ""]
        for loc in s.locations:
            out.append(f"{h2} {loc.name} ({loc.interior_exterior})")
            out += _fields(loc, SET_FIELDS)
            for st in (x for x in s.states if x.location_id == loc.location_id):
                out.append(f"- state `{st.location_state_id}`:")
                out += _fields(st, SET_STATE_FIELDS, "  ")
            out.append("")
    elif which == "props" and pkg.prop_sheet:
        s = pkg.prop_sheet
        out += [f"{h1} Prop sheet (schema {s.schema_version})", ""]
        for p in s.props:
            out.append(f"{h2} {p.name}")
            out += _fields(p, PROP_FIELDS)
            for st in (x for x in s.states if x.prop_id == p.prop_id):
                out.append(f"- state `{st.prop_state_id}`:")
                out += _fields(st, PROP_STATE_FIELDS, "  ")
            out.append("")
    return out


def _sheets(pkg: ProductionPackage) -> list[str]:
    return [line for which in ("cast", "sets", "props") for line in sheet_md(pkg, which)]
