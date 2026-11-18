"""Hand edits to a package's design text, in place.

A package revision is normally immutable; this is the one exception. The user edits a
cast, set or prop sheet entry (and the variant's one-line description in the story
bible), and the asset requirements built from that text are rebuilt the way the pipeline
builds them, so new image prompts use the edit. Images already drawn are left as they
are: they were drawn from the earlier text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .models.package import ChangeLogEntry, ProductionPackage
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
        )
    )
    return edit


def _text(req) -> tuple[str, list[str], list[str]]:
    return (req.design_brief, req.must_keep, req.design_freedom)
