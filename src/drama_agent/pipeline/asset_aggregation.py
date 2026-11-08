"""Derive and merge asset requirements from the shot list (spec section 6D, D3 rules 1-3).

The model writes design text; the program owns the reuse units, used_by links, merging and
dependency graph so these invariants hold by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.assets import (
    AssetRequirement,
    AssetRequirements,
    AssetType,
    Deliverable,
    StyleBrief,
    SubjectRef,
    UsedBy,
)
from ..models.story import StoryBible
from ..models.storyboard import Shot


@dataclass(frozen=True)
class ReuseUnit:
    """The key two requirements must share to be merged into one."""

    asset_type: AssetType
    entity_type: str
    entity_id: str
    variant_id: str | None

    @property
    def requirement_id(self) -> str:
        if self.asset_type == "shot_reference":
            return f"ar_shot_{self.entity_id}"
        suffix = f"_{self.variant_id}" if self.variant_id else ""
        return f"ar_{self.asset_type}_{self.entity_id}{suffix}"


def shot_reference_unit(shot: Shot) -> ReuseUnit:
    return ReuseUnit("shot_reference", "location", shot.shot_id, None)


def required_units_for_shot(shot: Shot) -> list[ReuseUnit]:
    """Visual assets a shot needs. Voice-only characters are not visible and need none."""
    units: list[ReuseUnit] = []
    for ref in shot.character_look_refs:
        units.append(ReuseUnit("character_identity", "character", ref.character_id, None))
        units.append(ReuseUnit("character_look", "character", ref.character_id, ref.look_id))
    units.append(ReuseUnit("location", "location", shot.location_id, shot.location_state_id))
    for ref in shot.prop_state_refs:
        units.append(ReuseUnit("prop", "prop", ref.prop_id, ref.prop_state_id))
    if shot.needs_shot_reference:
        units.append(shot_reference_unit(shot))
    return units


def _default_brief(unit: ReuseUnit, bible: StoryBible) -> tuple[str, list[Deliverable]] | None:
    """Default design text, or None when the unit names an ID missing from the bible."""
    try:
        return _default_brief_or_raise(unit, bible)
    except KeyError:
        return None


def _default_brief_or_raise(unit: ReuseUnit, bible: StoryBible) -> tuple[str, list[Deliverable]]:
    chars = {c.character_id: c for c in bible.characters}
    looks = {lk.look_id: lk for lk in bible.looks}
    locs = {loc.location_id: loc for loc in bible.locations}
    loc_states = {s.location_state_id: s for s in bible.location_states}
    props = {p.prop_id: p for p in bible.props}
    prop_states = {s.prop_state_id: s for s in bible.prop_states}
    rid = unit.requirement_id

    match unit.asset_type:
        case "character_identity":
            c = chars[unit.entity_id]
            return f"{c.name}: {c.appearance}", [
                Deliverable(deliverable_id=f"d_{rid}_face", view="front face", purpose="identity")
            ]
        case "character_look":
            c, lk = chars[unit.entity_id], looks[unit.variant_id or ""]
            return f"{c.name} / {lk.description}", [
                Deliverable(deliverable_id=f"d_{rid}_full", view="full body", purpose="costume")
            ]
        case "location":
            loc, st = locs[unit.entity_id], loc_states[unit.variant_id or ""]
            return f"{loc.name}: {loc.layout}; {st.description}", [
                Deliverable(deliverable_id=f"d_{rid}_wide", view="wide", purpose="space"),
                Deliverable(deliverable_id=f"d_{rid}_layout", view="top-down", purpose="blocking"),
            ]
        case "prop":
            p, st = props[unit.entity_id], prop_states[unit.variant_id or ""]
            return f"{p.name}: {p.description}; {st.description}", [
                Deliverable(deliverable_id=f"d_{rid}_detail", view="detail", purpose="identify")
            ]
        case "shot_reference":
            return f"Composition reference for {unit.entity_id}", [
                Deliverable(deliverable_id=f"d_{rid}_frame", view="first frame", purpose="layout")
            ]
    raise ValueError(unit.asset_type)


def aggregate_asset_requirements(
    bible: StoryBible,
    shots: list[Shot],
    style_brief: StyleBrief,
    design_overrides: dict[str, AssetRequirement] | None = None,
) -> AssetRequirements:
    """Build merged requirements and write requirement IDs back onto each shot.

    design_overrides lets the model supply richer design_brief / must_keep / design_freedom
    keyed by requirement_id; structural fields (used_by, depends_on, subject_refs) stay
    program-owned.
    """
    overrides = design_overrides or {}
    items: dict[str, AssetRequirement] = {}
    unit_of: dict[str, ReuseUnit] = {}

    for shot in shots:
        shot.asset_requirement_ids = []
        for unit in required_units_for_shot(shot):
            rid = unit.requirement_id
            used = UsedBy(episode_id=shot.episode_id, scene_id=shot.scene_id, shot_id=shot.shot_id)
            if rid not in items:
                made = _default_brief(unit, bible)
                if made is None:
                    # Unknown ID: leave it to check_references, which feeds it back to the
                    # model for repair instead of crashing the job.
                    continue
                brief, deliverables = made
                ref_entity = shot.location_id if unit.asset_type == "shot_reference" else None
                subject = SubjectRef(
                    entity_type=unit.entity_type,  # type: ignore[arg-type]
                    entity_id=ref_entity or unit.entity_id,
                    variant_id=shot.location_state_id if ref_entity else unit.variant_id,
                )
                items[rid] = AssetRequirement(
                    requirement_id=rid,
                    asset_type=unit.asset_type,
                    subject_refs=[subject],
                    design_brief=brief,
                    used_by=[used],
                    deliverables=deliverables,
                )
                unit_of[rid] = unit
            elif used not in items[rid].used_by:
                items[rid].used_by.append(used)
            if rid not in shot.asset_requirement_ids:
                shot.asset_requirement_ids.append(rid)

    # Dependencies (D3 rule 3): look -> identity; shot_reference -> everything else it uses.
    for shot in shots:
        for rid in shot.asset_requirement_ids:
            unit = unit_of[rid]
            req = items[rid]
            if unit.asset_type == "character_look":
                dep = ReuseUnit("character_identity", "character", unit.entity_id, None)
                deps = [dep.requirement_id]
            elif unit.asset_type == "shot_reference":
                deps = [r for r in shot.asset_requirement_ids if r != rid]
            else:
                deps = []
            for d in deps:
                if d not in req.depends_on:
                    req.depends_on.append(d)

    for rid, override in overrides.items():
        if rid in items:
            req = items[rid]
            req.design_brief = override.design_brief or req.design_brief
            req.must_keep = override.must_keep or req.must_keep
            req.design_freedom = override.design_freedom or req.design_freedom
            req.source_refs = override.source_refs or req.source_refs
            req.deliverables = override.deliverables or req.deliverables

    for req in items.values():
        if req.deliverables and any(d.required for d in req.deliverables):
            req.requirement_status = "ready"

    return AssetRequirements(style_brief=style_brief, items=list(items.values()))
