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
from ..models.design_sheets import (
    CastLook,
    CastMember,
    CastSheet,
    PropItem,
    PropSheet,
    PropVariant,
    SetLocation,
    SetSheet,
    SetState,
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


@dataclass
class Default:
    brief: str
    deliverables: list[Deliverable]
    must_keep: list[str]
    design_freedom: list[str]


def _join(*parts: str) -> str:
    return "；".join(p.strip() for p in parts if p and p.strip())


def _default_brief(
    unit: ReuseUnit, bible: StoryBible, sheets: DesignSheets | None = None
) -> Default | None:
    """Default design text, or None when the unit names an ID missing from the bible."""
    try:
        return _default_brief_or_raise(unit, bible, sheets or DesignSheets())
    except KeyError:
        return None


def _legacy(obj: object, *keys: str) -> list[str]:
    """Visual text kept from packages made before the design sheets."""
    old = getattr(obj, "legacy", None) or {}
    return [old[k] for k in keys if old.get(k)]


def _default_brief_or_raise(unit: ReuseUnit, bible: StoryBible, sheets: DesignSheets) -> Default:
    """Design text comes from the design sheets; the bible supplies names and each
    variant's one-line description. Without a sheet entry (old packages) it falls back to
    the old visual fields, then to the name."""
    chars = {c.character_id: c for c in bible.characters}
    looks = {lk.look_id: lk for lk in bible.looks}
    locs = {loc.location_id: loc for loc in bible.locations}
    loc_states = {s.location_state_id: s for s in bible.location_states}
    props = {p.prop_id: p for p in bible.props}
    prop_states = {s.prop_state_id: s for s in bible.prop_states}
    design = sheets.entries()
    rid = unit.requirement_id

    def d(suffix: str, view: str, purpose: str) -> Deliverable:
        return Deliverable(deliverable_id=f"d_{rid}_{suffix}", view=view, purpose=purpose)

    match unit.asset_type:
        case "character_identity":
            c = chars[unit.entity_id]
            m = design.get(c.character_id)
            if isinstance(m, CastMember):
                parts = [m.age_range, m.gender_presentation, m.build_and_height, m.face, m.hair,
                         m.distinguishing_features]  # fmt: skip
                keep, freedom = list(m.must_keep), list(m.design_freedom)
            else:
                parts, keep, freedom = _legacy(c, "appearance"), [], []
            return Default(
                f"{c.name}：" + _join(*parts) if parts else c.name,
                [d("face", "front face", "identity")],
                keep,
                freedom,
            )
        case "character_look":
            c, lk = chars[unit.entity_id], looks[unit.variant_id or ""]
            m, e = design.get(c.character_id), design.get(lk.look_id)
            parts = [lk.description]
            keep: list[str] = []
            if isinstance(e, CastLook):
                parts += [e.wardrobe, e.hair_and_makeup, e.condition]
                keep += e.must_keep
            else:
                parts += _legacy(c, "costume") + _legacy(lk, "diff_from_base")
            if isinstance(m, CastMember):
                # Same person: the look must keep what identifies the character (a scar).
                parts.append(m.distinguishing_features)
                keep += [k for k in m.must_keep if k not in keep]
            return Default(
                f"{c.name} / " + _join(*parts), [d("full", "full body", "costume")], keep, []
            )
        case "location":
            loc, st = locs[unit.entity_id], loc_states[unit.variant_id or ""]
            base, state = design.get(loc.location_id), design.get(st.location_state_id)
            parts = [st.description]  # the variant's own line is always kept
            keep, freedom = [], []
            if isinstance(base, SetLocation):
                # Lighting and entrances matter for the top-down blocking deliverable.
                parts += [base.layout, base.key_furnishings, base.materials_and_textures,
                          base.color_palette, base.lighting, base.entrances_and_blocking]  # fmt: skip
                keep += base.must_keep
                freedom += base.design_freedom
            else:
                parts += _legacy(loc, "layout", "furnishings", "lighting", "entrances")
            if isinstance(state, SetState):
                parts += [state.time_of_day, state.lighting, state.dressing_changes,
                          state.condition, state.changes_from_base]  # fmt: skip
                keep += state.must_keep
            else:
                parts += _legacy(st, "diff_from_base")
            return Default(
                f"{loc.name}：" + _join(*parts),
                [d("wide", "wide", "space"), d("layout", "top-down", "blocking")],
                keep,
                freedom,
            )
        case "prop":
            p, st = props[unit.entity_id], prop_states[unit.variant_id or ""]
            base, state = design.get(p.prop_id), design.get(st.prop_state_id)
            parts = [st.description]
            keep, freedom = [], []
            if isinstance(base, PropItem):
                parts += [base.size_and_scale, base.materials, base.color_and_finish,
                          base.distinguishing_marks]  # fmt: skip
                keep += base.must_keep
                freedom += base.design_freedom
            else:
                parts += _legacy(p, "description")
            if isinstance(state, PropVariant):
                parts += [state.condition, state.changes_from_base]
                keep += state.must_keep
            else:
                parts += _legacy(st, "diff_from_base")
            return Default(
                f"{p.name}：" + _join(*parts), [d("detail", "detail", "identify")], keep, freedom
            )
        case "shot_reference":
            return Default(
                f"Composition reference for {unit.entity_id}",
                [d("frame", "first frame", "layout")],
                [],
                [],
            )
    raise ValueError(unit.asset_type)


@dataclass
class DesignSheets:
    cast: CastSheet | None = None
    sets: SetSheet | None = None
    props: PropSheet | None = None

    def entries(self) -> dict[str, object]:
        """Entries of all three sheets, keyed by the story-bible ID they describe."""
        out: dict[str, object] = {}
        for sheet in (self.cast, self.sets, self.props):
            if sheet is not None:
                out |= sheet.entries()
        return out


def aggregate_asset_requirements(
    bible: StoryBible,
    shots: list[Shot],
    style_brief: StyleBrief,
    design_overrides: dict[str, AssetRequirement] | None = None,
    sheets: DesignSheets | None = None,
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
                made = _default_brief(unit, bible, sheets)
                if made is None:
                    # Unknown ID: leave it to check_references, which feeds it back to the
                    # model for repair instead of crashing the job.
                    continue
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
                    design_brief=made.brief,
                    must_keep=made.must_keep,
                    design_freedom=made.design_freedom,
                    used_by=[used],
                    deliverables=made.deliverables,
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
