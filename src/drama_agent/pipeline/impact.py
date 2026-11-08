"""Revision impact analysis (spec section 9 impact rules, section 6D D3 rule 6).

Decides which objects a targeted edit may touch and which asset requirements become stale,
before any regeneration runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ProductionPackage
from ..validation import PackageIndex


@dataclass
class Impact:
    impacted_ids: list[str] = field(default_factory=list)
    stale_requirement_ids: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    # Locked objects inside the revision scope. Not conflicts by themselves: regeneration
    # must keep them unchanged, and check_locks enforces that afterwards.
    locked_in_scope: list[str] = field(default_factory=list)
    unknown_targets: list[str] = field(default_factory=list)


def _later_scenes(pkg: ProductionPackage, scene_id: str) -> list[str]:
    ordered = [s.scene_id for ep in pkg.episodes for s in ep.scenes]
    return ordered[ordered.index(scene_id) :] if scene_id in ordered else []


def _scene_of(pkg: ProductionPackage, idx: PackageIndex, object_id: str) -> str | None:
    """The scene that contains a scene-level object (scene, beat, dialogue line, shot)."""
    if object_id in idx.scenes:
        return object_id
    if object_id in idx.beats:
        return idx.beats[object_id]
    if object_id in idx.shots:
        return idx.shots[object_id].scene_id
    if object_id in idx.dialogue:
        for sc in idx.scenes.values():
            if any(b.dialogue and b.dialogue.dialogue_id == object_id for b in sc.beats):
                return sc.scene_id
    return None


def _requirements_for_variant(pkg: ProductionPackage, variant_id: str) -> set[str]:
    return {
        r.requirement_id
        for r in pkg.asset_requirements.items
        if any(s.variant_id == variant_id for s in r.subject_refs)
    }


def _requirements_for_entity_identity(pkg: ProductionPackage, entity_id: str) -> set[str]:
    return {
        r.requirement_id
        for r in pkg.asset_requirements.items
        if r.asset_type in ("character_identity", "location", "prop")
        and any(s.entity_id == entity_id for s in r.subject_refs)
    }


def _with_dependents(pkg: ProductionPackage, seeds: set[str]) -> set[str]:
    """Transitively add requirements that depend on any seed."""
    result = set(seeds)
    changed = True
    while changed:
        changed = False
        for r in pkg.asset_requirements.items:
            if r.requirement_id not in result and set(r.depends_on) & result:
                result.add(r.requirement_id)
                changed = True
    return result


def analyze_impact(
    pkg: ProductionPackage,
    target_ids: list[str],
    locked_ids: list[str],
    visual_change: bool = False,
) -> Impact:
    """visual_change: the edit alters how a character/location/prop looks, not only story
    facts. Looks, location states, prop states and requirements are always visual."""
    idx = PackageIndex.build(pkg)
    reqs = pkg.asset_requirements.by_id()
    impacted: set[str] = set()
    stale: set[str] = set()
    impact = Impact()

    for tid in target_ids:
        impacted.add(tid)
        if tid in idx.shots:
            # Camera/blocking change: this shot, neighbours for continuity, its composition ref.
            sh = idx.shots[tid]
            ep_shots = sorted(
                (s for s in pkg.shot_list if s.episode_id == sh.episode_id),
                key=lambda s: s.start_ms,
            )
            i = [s.shot_id for s in ep_shots].index(tid)
            impacted.update(s.shot_id for s in ep_shots[max(i - 1, 0) : i + 2])
            stale.update(
                rid
                for rid in sh.asset_requirement_ids
                if reqs.get(rid) and reqs[rid].asset_type == "shot_reference"
            )
        elif tid in idx.dialogue:
            # Dialogue-only edit: timing, cueing shots, speaker knowledge. No image goes stale.
            ep_id = idx.dialogue_episode[tid]
            impacted.add(ep_id)
            impacted.update(
                s.shot_id
                for s in pkg.shot_list
                if any(c.dialogue_id == tid for c in s.dialogue_cues)
            )
        elif tid in idx.scenes:
            impacted.update(_later_scenes(pkg, tid))
        elif tid in idx.episodes:
            # This episode and every later one (works even if an episode has no scenes).
            i = [e.episode_id for e in pkg.episodes].index(tid)
            impacted.update(s.scene_id for ep in pkg.episodes[i:] for s in ep.scenes)
        elif tid in idx.characters or tid in idx.locations or tid in idx.props:
            # Identity / goal / key fact: every scene from first appearance onward.
            prop_scenes = {
                sh.scene_id
                for sh in pkg.shot_list
                if any(r.prop_id == tid for r in sh.prop_state_refs)
            }
            appears = [
                s.scene_id
                for ep in pkg.episodes
                for s in ep.scenes
                if tid in s.character_ids or tid == s.location_id or s.scene_id in prop_scenes
            ]
            if appears:
                impacted.update(_later_scenes(pkg, appears[0]))
            if visual_change:
                stale |= _with_dependents(pkg, _requirements_for_entity_identity(pkg, tid))
        elif tid in idx.looks or tid in idx.location_states or tid in idx.prop_states:
            stale |= _with_dependents(pkg, _requirements_for_variant(pkg, tid))
        elif tid in reqs:
            stale |= _with_dependents(pkg, {tid})
        else:
            impact.unknown_targets.append(tid)

    for rid in stale:
        impacted.update(u.shot_id for u in reqs[rid].used_by)

    locked = set(locked_ids) | set(pkg.locked_ids)
    impact.conflicts = sorted(set(target_ids) & locked)
    impact.locked_in_scope = sorted(
        lid
        for lid in locked - set(target_ids)
        if lid in impacted or _scene_of(pkg, idx, lid) in impacted
    )
    impact.impacted_ids = sorted(impacted)
    impact.stale_requirement_ids = sorted(stale)
    return impact
