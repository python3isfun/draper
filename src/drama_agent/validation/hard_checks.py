"""Deterministic hard checks (spec section 8).

A package may be marked completed only when run_hard_checks returns no issues. Prompts never
replace these checks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from ..models import Issue, JobOptions, ProductionPackage
from ..pipeline.asset_aggregation import required_units_for_shot, shot_reference_unit
from .index import PackageIndex, design_entries, lockable_objects
from .prose import find_prose_leaks

Check = Callable[[ProductionPackage, PackageIndex, JobOptions], list[Issue]]


def _issue(check: str, object_id: str, evidence: str, fix_scope: str = "") -> Issue:
    return Issue(check=check, object_id=object_id, evidence=evidence, fix_scope=fix_scope)


# ---------------------------------------------------------------------------
# Structure


def check_unique_ids(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    return [_issue("unique_ids", d, "ID defined more than once") for d in idx.duplicates]


def check_episode_structure(
    pkg: ProductionPackage, idx: PackageIndex, opts: JobOptions
) -> list[Issue]:
    issues: list[Issue] = []
    if len(pkg.episodes) != opts.episode_count:
        issues.append(
            _issue("episode_count", pkg.package_id, f"{len(pkg.episodes)} episodes, expected 3")
        )
    for ep in pkg.episodes:
        n_scenes = len(ep.scenes)
        rng = opts.scenes_per_episode
        if not rng.min <= n_scenes <= rng.max:
            issues.append(
                _issue("scenes_per_episode", ep.episode_id, f"{n_scenes} scenes", ep.episode_id)
            )
        n_chars = len(ep.character_ids())
        rng = opts.characters_per_episode
        if not rng.min <= n_chars <= rng.max:
            issues.append(_issue("characters_per_episode", ep.episode_id, f"{n_chars} characters"))
    if opts.story_scope == "series_opening" and not pkg.story_bible.future_direction:
        issues.append(
            _issue("future_direction", "story_bible", "series_opening needs future_direction")
        )
    return issues


# ---------------------------------------------------------------------------
# Timeline (spec section 7)


def check_timeline(pkg: ProductionPackage, idx: PackageIndex, opts: JobOptions) -> list[Issue]:
    issues: list[Issue] = []
    total = opts.episode_duration_ms
    by_episode: dict[str, list] = defaultdict(list)
    for sh in pkg.shot_list:
        by_episode[sh.episode_id].append(sh)

    for ep in pkg.episodes:
        shots = sorted(by_episode.get(ep.episode_id, []), key=lambda s: s.start_ms)
        if not shots:
            issues.append(_issue("timeline", ep.episode_id, "episode has no shots"))
            continue
        cursor = 0
        for sh in shots:
            if sh.start_ms != cursor:
                kind = "gap" if sh.start_ms > cursor else "overlap"
                issues.append(
                    _issue(
                        "timeline", sh.shot_id, f"{kind}: starts {sh.start_ms}, expected {cursor}"
                    )
                )
            cursor = sh.end_ms
        if cursor != total:
            issues.append(
                _issue("timeline", ep.episode_id, f"ends at {cursor}ms, expected {total}")
            )

        # Scene durations must equal the contiguous span of their shots.
        for sc in ep.scenes:
            sc_shots = [s for s in shots if s.scene_id == sc.scene_id]
            if not sc_shots:
                issues.append(_issue("scene_duration", sc.scene_id, "scene has no shots"))
                continue
            span = sum(s.duration_ms for s in sc_shots)
            contiguous = sc_shots[-1].end_ms - sc_shots[0].start_ms == span
            if span != sc.target_duration_ms or not contiguous:
                issues.append(
                    _issue(
                        "scene_duration",
                        sc.scene_id,
                        f"shots cover {span}ms (contiguous={contiguous}), "
                        f"scene target {sc.target_duration_ms}ms",
                    )
                )

    timing_counts: dict[str, int] = defaultdict(int)
    for t in pkg.timing:
        timing_counts[t.episode_id] += 1
    for ep in pkg.episodes:
        n = timing_counts.get(ep.episode_id, 0)
        if n != 1:
            issues.append(_issue("timing_record", ep.episode_id, f"{n} timing records, expected 1"))
    for ep_id in timing_counts.keys() - idx.episodes.keys():
        issues.append(_issue("timing_record", ep_id, "timing record for unknown episode"))
    return issues


def check_dialogue_schedule(
    pkg: ProductionPackage, idx: PackageIndex, opts: JobOptions
) -> list[Issue]:
    issues: list[Issue] = []
    for timing in pkg.timing:
        ep_id = timing.episode_id
        expected = {d for d, e in idx.dialogue_episode.items() if e == ep_id}
        seen: dict[str, int] = defaultdict(int)
        for entry in timing.dialogue_schedule:
            seen[entry.dialogue_id] += 1
            if entry.dialogue_id not in expected:
                issues.append(
                    _issue("dialogue_schedule", entry.dialogue_id, f"not a line of {ep_id}")
                )
            if entry.end_ms > opts.episode_duration_ms or entry.start_ms >= entry.end_ms:
                issues.append(_issue("dialogue_schedule", entry.dialogue_id, "bad time range"))
        for d in expected:
            if seen[d] == 0:
                issues.append(_issue("dialogue_schedule", d, "dialogue not scheduled"))
            elif seen[d] > 1:
                issues.append(_issue("dialogue_schedule", d, f"scheduled {seen[d]} times"))

        ordered = sorted(timing.dialogue_schedule, key=lambda e: e.start_ms)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if b.start_ms >= a.end_ms:
                    break  # sorted by start: no later line can overlap `a`
                if not (a.overlap_allowed or b.overlap_allowed):
                    issues.append(
                        _issue("dialogue_overlap", b.dialogue_id, f"overlaps {a.dialogue_id}")
                    )

        if timing.spoken_timing_verified:
            # Not wired to any voice measurement in V1, so this cannot legitimately be true.
            issues.append(
                _issue("spoken_timing", ep_id, "spoken_timing_verified set without measurement")
            )

    schedule = {e.dialogue_id: e for t in pkg.timing for e in t.dialogue_schedule}
    for sh in pkg.shot_list:
        for cue in sh.dialogue_cues:
            entry = schedule.get(cue.dialogue_id)
            if entry is None:
                issues.append(_issue("dialogue_cue", sh.shot_id, f"{cue.dialogue_id} unscheduled"))
                continue
            inside_line = entry.start_ms <= cue.start_ms < cue.end_ms <= entry.end_ms
            inside_shot = sh.start_ms <= cue.start_ms and cue.end_ms <= sh.end_ms
            if not (inside_line and inside_shot):
                issues.append(
                    _issue("dialogue_cue", sh.shot_id, f"cue {cue.dialogue_id} out of range")
                )
    return issues


# ---------------------------------------------------------------------------
# References


def check_references(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    issues: list[Issue] = []
    bad = issues.append

    for lk in idx.looks.values():
        if lk.character_id not in idx.characters:
            bad(_issue("ref", lk.look_id, f"unknown character {lk.character_id}"))
    for ls in idx.location_states.values():
        if ls.location_id not in idx.locations:
            bad(_issue("ref", ls.location_state_id, f"unknown location {ls.location_id}"))
    for ps in idx.prop_states.values():
        if ps.prop_id not in idx.props:
            bad(_issue("ref", ps.prop_state_id, f"unknown prop {ps.prop_id}"))

    for sc in idx.scenes.values():
        if sc.location_id not in idx.locations:
            bad(_issue("ref", sc.scene_id, f"undeclared location {sc.location_id}"))
        st = idx.location_states.get(sc.location_state_id)
        if st is None or st.location_id != sc.location_id:
            bad(_issue("ref", sc.scene_id, f"bad location_state {sc.location_state_id}"))
        for cid in sc.character_ids:
            if cid not in idx.characters:
                bad(_issue("ref", sc.scene_id, f"undeclared character {cid}"))
        for line in sc.dialogue_lines():
            if line.character_id not in sc.character_ids:
                bad(_issue("ref", line.dialogue_id, f"speaker {line.character_id} not in scene"))

    for sh in pkg.shot_list:
        sid = sh.shot_id
        sc = idx.scenes.get(sh.scene_id)
        if sc is None or idx.scene_episode.get(sh.scene_id) != sh.episode_id:
            bad(_issue("ref", sid, f"scene {sh.scene_id} not in episode {sh.episode_id}"))
            continue
        if sh.location_id != sc.location_id or sh.location_state_id != sc.location_state_id:
            bad(_issue("ref", sid, "shot location/state differs from its scene"))
        for cid in sh.character_ids:
            if cid not in sc.character_ids:
                bad(_issue("ref", sid, f"character {cid} not declared in scene"))
        look_chars = [r.character_id for r in sh.character_look_refs]
        if sorted(look_chars) != sorted(set(sh.character_ids)):
            bad(_issue("ref", sid, "each visible character needs exactly one look ref"))
        for r in sh.character_look_refs:
            lk = idx.looks.get(r.look_id)
            if lk is None or lk.character_id != r.character_id:
                bad(_issue("ref", sid, f"look {r.look_id} does not belong to {r.character_id}"))
        for r in sh.prop_state_refs:
            ps = idx.prop_states.get(r.prop_state_id)
            if ps is None or ps.prop_id != r.prop_id:
                bad(_issue("ref", sid, f"prop state {r.prop_state_id} not of {r.prop_id}"))
        for b in sh.source_beat_ids:
            if idx.beats.get(b) != sh.scene_id:
                bad(_issue("ref", sid, f"beat {b} is not in scene {sh.scene_id}"))
        for cue in sh.dialogue_cues:
            if idx.dialogue_episode.get(cue.dialogue_id) != sh.episode_id:
                bad(_issue("ref", sid, f"dialogue {cue.dialogue_id} not in {sh.episode_id}"))
    return issues


# ---------------------------------------------------------------------------
# Asset requirements (spec section 6D, 8)


def check_asset_requirements(
    pkg: ProductionPackage, idx: PackageIndex, _: JobOptions
) -> list[Issue]:
    issues: list[Issue] = []
    bad = issues.append
    reqs = pkg.asset_requirements.items
    by_id = {}
    deliverable_ids: set[str] = set()

    for r in reqs:
        if r.requirement_id in by_id:
            bad(_issue("asset_unique", r.requirement_id, "duplicate requirement_id"))
        by_id[r.requirement_id] = r
        for d in r.deliverables:
            if d.deliverable_id in deliverable_ids:
                bad(_issue("asset_unique", d.deliverable_id, "duplicate deliverable_id"))
            deliverable_ids.add(d.deliverable_id)

    # Subject refs must point at real entities, and variants must belong to them.
    variant_tables = {
        "character": (idx.characters, idx.looks, "character_id"),
        "location": (idx.locations, idx.location_states, "location_id"),
        "prop": (idx.props, idx.prop_states, "prop_id"),
    }
    for r in reqs:
        for ref in r.subject_refs:
            entities, variants, owner_field = variant_tables[ref.entity_type]
            if ref.entity_id not in entities:
                bad(_issue("asset_subject", r.requirement_id, f"unknown entity {ref.entity_id}"))
            elif ref.variant_id is not None:
                v = variants.get(ref.variant_id)
                if v is None or getattr(v, owner_field) != ref.entity_id:
                    bad(
                        _issue(
                            "asset_subject",
                            r.requirement_id,
                            f"variant {ref.variant_id} not of {ref.entity_id}",
                        )
                    )

    # Unmerged duplicates of the same reuse unit.
    seen_units: dict[tuple, str] = {}
    for r in reqs:
        if r.asset_type == "shot_reference":
            continue
        key = (
            r.asset_type,
            tuple((s.entity_type, s.entity_id, s.variant_id) for s in r.subject_refs),
        )
        if key in seen_units:
            bad(_issue("asset_merge", r.requirement_id, f"same reuse unit as {seen_units[key]}"))
        seen_units[key] = r.requirement_id

    # used_by <-> shot.asset_requirement_ids must agree in both directions.
    for r in reqs:
        for u in r.used_by:
            sh = idx.shots.get(u.shot_id)
            if sh is None or sh.scene_id != u.scene_id or sh.episode_id != u.episode_id:
                bad(_issue("asset_used_by", r.requirement_id, f"bad used_by {u.shot_id}"))
            elif r.requirement_id not in sh.asset_requirement_ids:
                bad(_issue("asset_used_by", r.requirement_id, f"{u.shot_id} does not list it"))
    for sh in pkg.shot_list:
        for rid in sh.asset_requirement_ids:
            r = by_id.get(rid)
            if r is None:
                bad(_issue("asset_used_by", sh.shot_id, f"unknown requirement {rid}"))
            elif sh.shot_id not in {u.shot_id for u in r.used_by}:
                bad(_issue("asset_used_by", sh.shot_id, f"{rid}.used_by misses this shot"))

    # Coverage: every visible unit in a shot has a required requirement on that shot.
    for sh in pkg.shot_list:
        attached = [by_id[rid] for rid in sh.asset_requirement_ids if rid in by_id]
        for unit in required_units_for_shot(sh):
            if unit == shot_reference_unit(sh):
                match = [r for r in attached if r.asset_type == "shot_reference"]
            else:
                match = [
                    r
                    for r in attached
                    if r.asset_type == unit.asset_type
                    and any(
                        s.entity_id == unit.entity_id and s.variant_id == unit.variant_id
                        for s in r.subject_refs
                    )
                ]
            if not any(r.priority == "required" for r in match):
                bad(
                    _issue(
                        "asset_coverage",
                        sh.shot_id,
                        f"no required {unit.asset_type} for {unit.entity_id}/{unit.variant_id}",
                    )
                )

    # Dependencies: valid, acyclic, and the mandatory edges exist.
    for r in reqs:
        for d in r.depends_on:
            if d not in by_id:
                bad(_issue("asset_depends_on", r.requirement_id, f"unknown dependency {d}"))
        if r.asset_type == "character_look":
            ids = {s.entity_id for s in r.subject_refs}
            has_identity = any(
                by_id.get(d) is not None
                and by_id[d].asset_type == "character_identity"
                and {s.entity_id for s in by_id[d].subject_refs} & ids
                for d in r.depends_on
            )
            if not has_identity:
                bad(_issue("asset_depends_on", r.requirement_id, "look lacks identity dependency"))
        if r.asset_type == "shot_reference":
            for u in r.used_by:
                sh = idx.shots.get(u.shot_id)
                if sh is None:
                    continue
                needed = set(sh.asset_requirement_ids) - {r.requirement_id}
                needed = {
                    n for n in needed if by_id.get(n) and by_id[n].asset_type != "shot_reference"
                }
                missing = needed - set(r.depends_on)
                if missing:
                    bad(
                        _issue(
                            "asset_depends_on",
                            r.requirement_id,
                            f"shot reference misses deps {sorted(missing)}",
                        )
                    )
    cycle = _find_cycle({r.requirement_id: r.depends_on for r in reqs})
    if cycle:
        bad(_issue("asset_depends_on", cycle[0], f"dependency cycle: {' -> '.join(cycle)}"))

    # Status: required items ready with at least one required deliverable.
    for r in reqs:
        if r.priority != "required":
            continue
        if r.requirement_status != "ready":
            bad(_issue("asset_status", r.requirement_id, f"required but {r.requirement_status}"))
        if not any(d.required for d in r.deliverables):
            bad(_issue("asset_status", r.requirement_id, "no required deliverable"))
    return issues


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == GREY:
                return stack[stack.index(nxt) :] + [nxt]
            if color[nxt] == WHITE and (found := visit(nxt)):
                return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in graph:
        if color[n] == WHITE and (found := visit(n)):
            return found
    return None


# ---------------------------------------------------------------------------
# Locks and metadata


def check_locks(base: ProductionPackage, new: ProductionPackage) -> list[Issue]:
    """Locked objects in the base revision must be byte-identical in the new one."""
    before, after = lockable_objects(base), lockable_objects(new)
    # A locked character/location/prop also locks its design-sheet entry (same ID).
    design_before, design_after = design_entries(base), design_entries(new)
    issues: list[Issue] = []
    for lid in base.locked_ids:
        if lid not in before:
            continue
        if lid not in after or before[lid].model_dump() != after[lid].model_dump():
            issues.append(_issue("locked", lid, "locked object was modified or removed"))
        elif lid in design_before and (
            lid not in design_after
            or design_before[lid].model_dump() != design_after[lid].model_dump()
        ):
            issues.append(_issue("locked", lid, "locked object's design sheet entry changed"))
    return issues


def check_entity_keys(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    """Maps keyed by character must use real character IDs.

    The live model invented keys such as `_production_notes` and `planning` to store its
    working notes inside story fields.
    """
    issues: list[Issue] = []
    bible = pkg.story_bible

    def keys(owner: str, label: str, mapping: dict) -> None:
        for k in mapping:
            if k not in idx.characters:
                issues.append(_issue("entity_key", owner, f"{label} key {k!r} is not a character"))

    keys("story_bible", "character_arcs", bible.character_arcs)
    for c in bible.characters:
        keys(c.character_id, "relationships", c.relationships)

    def state(owner: str, label: str, st) -> None:
        keys(owner, f"{label}.knowledge", st.knowledge)
        # Values are prose ("the cash envelope is back in his inside pocket"), not prop IDs: the spec registers only
        # plot-relevant props, so requiring IDs here failed a valid live script.
        keys(owner, f"{label}.possessions", st.possessions)

    for ep in pkg.episodes:
        state(ep.episode_id, "end_state", ep.end_state)
        for sc in ep.scenes:
            state(sc.scene_id, "start_state", sc.start_state)
            state(sc.scene_id, "end_state", sc.end_state)
    return issues


def check_prose(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    return find_prose_leaks(
        pkg.story_bible, pkg.episodes, pkg.shot_list, pkg.cast_sheet, pkg.set_sheet, pkg.prop_sheet
    )


# ---------------------------------------------------------------------------
# Design sheets: each covers exactly the story bible's entities of its kind


def _coverage(
    sheet: str,
    kind: str,
    entries: list[tuple[str, str | None]],
    expected: dict[str, str | None],
    parent_label: str = "",
) -> list[Issue]:
    """entries/expected: (id, parent id or None). Every expected ID exactly once, nothing
    unknown, and each variant under the same parent as in the story bible."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for eid, parent in entries:
        if eid in seen:
            issues.append(_issue("design_sheet", eid, f"{sheet}: {kind} described twice", sheet))
            continue
        seen.add(eid)
        if eid not in expected:
            issues.append(
                _issue("design_sheet", eid, f"{sheet}: {kind} is not in the story bible", sheet)
            )
        elif parent is not None and parent != expected[eid]:
            issues.append(
                _issue(
                    "design_sheet",
                    eid,
                    f"{sheet}: {kind} belongs to {expected[eid]}, not {parent} ({parent_label})",
                    sheet,
                )
            )
    for eid in expected:
        if eid not in seen:
            issues.append(_issue("design_sheet", eid, f"{sheet}: {kind} has no entry", sheet))
    return issues


def check_cast_sheet(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    sheet = pkg.cast_sheet
    if sheet is None:
        return []  # package made before the sheets existed
    bible = pkg.story_bible
    issues = _coverage(
        "cast_sheet",
        "character",
        [(c.character_id, None) for c in sheet.characters],
        {c.character_id: None for c in bible.characters},
    )
    issues += _coverage(
        "cast_sheet",
        "look",
        [(lk.look_id, lk.character_id) for lk in sheet.looks],
        {lk.look_id: lk.character_id for lk in bible.looks},
        "character_id",
    )
    # A character marked as never seen must not appear in a shot.
    off_screen = {c.character_id for c in sheet.characters if not c.on_screen}
    for shot in pkg.shot_list:
        for ref in shot.character_look_refs:
            if ref.character_id in off_screen:
                issues.append(
                    _issue(
                        "design_sheet",
                        ref.character_id,
                        f"cast_sheet says on_screen=false, but shot {shot.shot_id} shows them",
                        "cast_sheet",
                    )
                )
    return issues


def check_set_sheet(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    sheet = pkg.set_sheet
    if sheet is None:
        return []
    bible = pkg.story_bible
    return _coverage(
        "set_sheet",
        "location",
        [(loc.location_id, None) for loc in sheet.locations],
        {loc.location_id: None for loc in bible.locations},
    ) + _coverage(
        "set_sheet",
        "location state",
        [(st.location_state_id, st.location_id) for st in sheet.states],
        {st.location_state_id: st.location_id for st in bible.location_states},
        "location_id",
    )


def check_prop_sheet(pkg: ProductionPackage, idx: PackageIndex, _: JobOptions) -> list[Issue]:
    sheet = pkg.prop_sheet
    if sheet is None:
        return []
    bible = pkg.story_bible
    return _coverage(
        "prop_sheet",
        "prop",
        [(p.prop_id, None) for p in sheet.props],
        {p.prop_id: None for p in bible.props},
    ) + _coverage(
        "prop_sheet",
        "prop state",
        [(st.prop_state_id, st.prop_id) for st in sheet.states],
        {st.prop_state_id: st.prop_id for st in bible.prop_states},
        "prop_id",
    )


ALL_CHECKS: tuple[Check, ...] = (
    check_unique_ids,
    check_episode_structure,
    check_timeline,
    check_dialogue_schedule,
    check_references,
    check_asset_requirements,
    check_entity_keys,
    check_prose,
    check_cast_sheet,
    check_set_sheet,
    check_prop_sheet,
)


def run_hard_checks(
    pkg: ProductionPackage,
    options: JobOptions,
    base: ProductionPackage | None = None,
) -> list[Issue]:
    idx = PackageIndex.build(pkg)
    issues = [i for check in ALL_CHECKS for i in check(pkg, idx, options)]
    if base is not None:
        issues.extend(check_locks(base, pkg))
    return issues


def compute_planned_timing_valid(pkg: ProductionPackage, options: JobOptions) -> dict[str, bool]:
    """Per episode: timeline contiguous, non-overlapping, totalling the episode duration."""
    idx = PackageIndex.build(pkg)
    failing = {i.object_id for i in check_timeline(pkg, idx, options)}
    result = {}
    for ep in pkg.episodes:
        ep_objects = (
            {ep.episode_id}
            | {s.scene_id for s in ep.scenes}
            | {sh.shot_id for sh in pkg.shot_list if sh.episode_id == ep.episode_id}
        )
        result[ep.episode_id] = not (failing & ep_objects)
    return result
