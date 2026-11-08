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
