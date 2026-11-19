"""Hand edits to a package's design text, in place.

A package revision is normally immutable; this is the one exception. The user edits a
cast, set or prop sheet entry (and the variant's one-line description in the story
bible), and the asset requirements built from that text are rebuilt the way the pipeline
builds them, so new image prompts use the edit. Images already drawn are left as they
are: they were drawn from the earlier text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .models.assets import AssetRequirement, SubjectRef
from .models.design_sheets import CastLook, SetState
from .models.package import ChangeLogEntry, ProductionPackage, utcnow
from .models.story import CharacterLook, LocationState
from .pipeline.asset_aggregation import DesignSheets, ReuseUnit, default_design

# Sheet list name -> the ID field of its entries.
_SHEET_LISTS: dict[str, tuple[tuple[str, str], ...]] = {
    "cast_sheet": (("characters", "character_id"), ("looks", "look_id")),
    "set_sheet": (("locations", "location_id"), ("states", "location_state_id")),
    "prop_sheet": (("props", "prop_id"), ("states", "prop_state_id")),
}
# Story-bible list -> (ID field, the one-line fields a hand edit may change). Only the
# variants' descriptions: they are part of the design text. A location's summary or a
# prop's story function is story text that no image prompt reads.
_BIBLE_LISTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "looks": ("look_id", ("description",)),
    "location_states": ("location_state_id", ("description",)),
    "prop_states": ("prop_state_id", ("description",)),
}
# The parent ID field of a variant entry (look, location state, prop state).
_PARENT_FIELDS = ("character_id", "location_id", "prop_id")
# Never editable by hand: identity and the name (the bible owns it).
_FIXED = {"name", "schema_version"}


class EditError(ValueError):
    """The edit is not valid (unknown or fixed field, wrong type)."""


class EntryLocked(EditError):
    """The entry, its parent, or a requirement built from it is locked."""


@dataclass
class EntryEdit:
    entry_id: str
    changed: list[str] = field(default_factory=list)  # "sheet.layout", "bible.description"
    requirement_ids: list[str] = field(default_factory=list)  # requirements rebuilt
    # Rebuilt requirements whose design text had been written by the model (an override
    # of the sheet-built text): the rebuild replaced that wording.
    replaced_written: list[str] = field(default_factory=list)


def _find_sheet_entry(pkg: ProductionPackage, entry_id: str) -> tuple[list, int] | None:
    for sheet_name, lists in _SHEET_LISTS.items():
        sheet = getattr(pkg, sheet_name)
        if sheet is None:
            continue
        for list_name, id_field in lists:
            items = getattr(sheet, list_name)
            for i, item in enumerate(items):
                if getattr(item, id_field) == entry_id:
                    return items, i
    return None


def _find_bible_entry(
    pkg: ProductionPackage, entry_id: str
) -> tuple[list, int, tuple[str, ...]] | None:
    for list_name, (id_field, editable) in _BIBLE_LISTS.items():
        items = getattr(pkg.story_bible, list_name)
        for i, item in enumerate(items):
            if getattr(item, id_field) == entry_id:
                return items, i, editable
    return None


def _apply(
    items: list, i: int, fields: dict[str, Any], editable: set[str], where: str
) -> list[str]:
    """Validate `fields` against the entry's model and replace it; returns changed names."""
    old: BaseModel = items[i]
    for k in fields:
        if k not in editable:
            raise EditError(f"{where}.{k} can't be edited here")
    data = old.model_dump() | fields
    try:
        new = type(old).model_validate(data)
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise EditError(f"{where}: {msgs}") from None
    changed = [k for k in fields if getattr(new, k) != getattr(old, k)]
    if changed:
        items[i] = new
    return [f"{where}.{k}" for k in changed]


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    """Trim text, and drop blank lines from lists (the UI sends one item per line)."""
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, str):
            v = v.strip()
        elif isinstance(v, list):
            v = [x.strip() if isinstance(x, str) else x for x in v]
            v = [x for x in v if x != ""]
        out[k] = v
    return out


def _affected(pkg: ProductionPackage, entry_id: str) -> list:
    """Requirements whose design text is built from this entry."""
    return [
        req
        for req in pkg.asset_requirements.items
        if req.asset_type != "shot_reference"
        and entry_id in (req.subject_refs[0].entity_id, req.subject_refs[0].variant_id)
    ]


def _design(pkg: ProductionPackage, req) -> tuple[str, list[str], list[str]] | None:
    ref = req.subject_refs[0]
    unit = ReuseUnit(req.asset_type, ref.entity_type, ref.entity_id, ref.variant_id)
    sheets = DesignSheets(cast=pkg.cast_sheet, sets=pkg.set_sheet, props=pkg.prop_sheet)
    made = default_design(unit, pkg.story_bible, sheets)
    return None if made is None else (made.brief, made.must_keep, made.design_freedom)


def edit_entry(
    pkg: ProductionPackage,
    entry_id: str,
    sheet: dict[str, Any] | None = None,
    bible: dict[str, Any] | None = None,
) -> EntryEdit:
    """Edit one entry of `pkg` in place. Raises KeyError for an unknown ID, EntryLocked
    when it (or its parent, or a requirement built from it) is locked, EditError for a bad
    field. Rebuilds the design text of the asset requirements of that entry."""
    sheet, bible = _clean(sheet or {}), _clean(bible or {})
    found_sheet = _find_sheet_entry(pkg, entry_id)
    found_bible = _find_bible_entry(pkg, entry_id)
    if found_sheet is None and found_bible is None:
        raise KeyError(f"no cast, set or prop entry {entry_id}")
    entry = found_sheet[0][found_sheet[1]] if found_sheet else found_bible[0][found_bible[1]]
    parents = {getattr(entry, f) for f in _PARENT_FIELDS if hasattr(entry, f)} - {entry_id}
    affected = _affected(pkg, entry_id)
    locked = set(pkg.locked_ids) & ({entry_id} | parents | {r.requirement_id for r in affected})
    if locked:
        raise EntryLocked(f"locked: {', '.join(sorted(locked))}")
    # Which requirements carry model-written text (differs from what the sheets build).
    written = {r.requirement_id for r in affected if _design(pkg, r) not in (None, _text(r))}

    edit = EntryEdit(entry_id)
    if sheet:
        if found_sheet is None:
            raise EditError(f"{entry_id} has no design sheet entry")
        items, i = found_sheet
        editable = {
            k for k in type(items[i]).model_fields if k not in _FIXED and not k.endswith("_id")
        }
        edit.changed += _apply(items, i, sheet, editable, "sheet")
    if bible:
        if found_bible is None:
            raise EditError(f"{entry_id} has no one-line story description to edit")
        items, i, editable = found_bible
        edit.changed += _apply(items, i, bible, set(editable), "bible")
    if not edit.changed:
        return edit

    for req in affected:
        made = _design(pkg, req)
        if made is None:
            continue
        req.design_brief, req.must_keep, req.design_freedom = made
        edit.requirement_ids.append(req.requirement_id)
        if req.requirement_id in written:
            edit.replaced_written.append(req.requirement_id)

    pkg.change_log.append(
        ChangeLogEntry(
            revision_id=pkg.revision_id,
            target_ids=[entry_id],
            changed_ids=[entry_id, *edit.requirement_ids],
            note="Edited by hand: " + ", ".join(edit.changed),
            at=utcnow(),
        )
    )
    return edit


def _text(req) -> tuple[str, list[str], list[str]]:
    return (req.design_brief, req.must_keep, req.design_freedom)


@dataclass
class AddedVariant:
    entry_id: str  # the new look_id or location_state_id
    requirement_id: str
    deliverable_ids: list[str]


def _slug(text: str) -> str:
    """Up to three ASCII words of a description, for an id: "torn evening dress" ->
    "torn_evening_dress"; "" when it has none (another language)."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:3])


def _new_id(prefix: str, owner: str, description: str, taken: set[str]) -> str:
    """ "lk_lila_torn_evening_dress" for Lila's ("ch_lila") new look; a number when taken."""
    key = re.sub(r"^(ch|loc)_", "", owner)
    base = f"{prefix}_{key}_{_slug(description) or 'new'}"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    return out


def add_variant(
    pkg: ProductionPackage, parent_id: str, description: str, sheet: dict[str, Any] | None = None
) -> AddedVariant:
    """Add a look to a character, or a state to a location, by hand, in place: its line in
    the story bible, its design sheet entry and the asset requirement its picture is drawn
    for (a look's full body, drawn from the face; a state's wide and top-down views). No
    shot wears it yet, so its requirement is used by none; a script revision may put it in
    scenes. Raises KeyError for an unknown parent, EntryLocked when the parent is locked,
    EditError for a bad field or a character with no face to draw a look from."""
    description = description.strip()
    sheet = _clean(sheet or {})
    if not description:
        raise EditError("describe it in a line")
    bible = pkg.story_bible
    is_char = any(c.character_id == parent_id for c in bible.characters)
    if not is_char and not any(loc.location_id == parent_id for loc in bible.locations):
        raise KeyError(f"no character or location {parent_id}")
    if parent_id in pkg.locked_ids:
        raise EntryLocked(f"locked: {parent_id}")
    reqs = pkg.asset_requirements.items
    taken = {lk.look_id for lk in bible.looks} | {
        s.location_state_id for s in bible.location_states
    }
    taken |= {r.requirement_id for r in reqs}

    if is_char:
        identity = ReuseUnit("character_identity", "character", parent_id, None).requirement_id
        if not any(r.requirement_id == identity for r in reqs):
            raise EditError(f"{parent_id} has no face picture in this story to draw a look from")
        vid = _new_id("lk", parent_id, description, taken)
        fields = {"wardrobe": "", "hair_and_makeup": "", "changes_from_base": description} | sheet
        entry: BaseModel = _validated(
            CastLook, fields | {"look_id": vid, "character_id": parent_id}
        )
        line: BaseModel = CharacterLook(
            look_id=vid, character_id=parent_id, description=description
        )
        unit = ReuseUnit("character_look", "character", parent_id, vid)
        bible.looks.append(line)  # type: ignore[arg-type]
        if pkg.cast_sheet is not None:
            pkg.cast_sheet.looks.append(entry)  # type: ignore[arg-type]
        depends = [identity]
    else:
        vid = _new_id("ls", parent_id, description, taken)
        fields = {"time_of_day": "", "lighting": "", "changes_from_base": description} | sheet
        entry = _validated(SetState, fields | {"location_state_id": vid, "location_id": parent_id})
        line = LocationState(location_state_id=vid, location_id=parent_id, description=description)
        unit = ReuseUnit("location", "location", parent_id, vid)
        bible.location_states.append(line)  # type: ignore[arg-type]
        if pkg.set_sheet is not None:
            pkg.set_sheet.states.append(entry)  # type: ignore[arg-type]
        depends = []

    sheets = DesignSheets(cast=pkg.cast_sheet, sets=pkg.set_sheet, props=pkg.prop_sheet)
    made = default_design(unit, bible, sheets)
    assert made is not None  # its bible entries were just added
    req = AssetRequirement(
        requirement_id=unit.requirement_id,
        asset_type=unit.asset_type,
        subject_refs=[
            SubjectRef(entity_type=unit.entity_type, entity_id=parent_id, variant_id=vid)
        ],  # type: ignore[arg-type]
        design_brief=made.brief,
        must_keep=made.must_keep,
        design_freedom=made.design_freedom,
        deliverables=made.deliverables,
        depends_on=depends,
        requirement_status="ready",
    )
    reqs.append(req)
    pkg.change_log.append(
        ChangeLogEntry(
            revision_id=pkg.revision_id,
            target_ids=[parent_id],
            changed_ids=[vid, req.requirement_id],
            note=f"Added by hand: {'look' if is_char else 'state'} {vid}",
            at=utcnow(),
        )
    )
    return AddedVariant(vid, req.requirement_id, [d.deliverable_id for d in req.deliverables])


def _validated(model: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    unknown = set(data) - set(model.model_fields)
    if unknown:
        raise EditError(
            f"unknown field{'s' if len(unknown) > 1 else ''}: {', '.join(sorted(unknown))}"
        )
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise EditError(msgs) from None
