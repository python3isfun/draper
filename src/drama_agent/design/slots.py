"""How design pictures depend on each other (anchors, parents), the board's slots, and
picks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import ProductionPackage
from ..models.images import GeneratedImage
from .prompts import _Slot, _slots, _subject, key_of


@dataclass
class _Graph:
    """How the deliverables depend on each other for design exploration.

    Anchors are explored first: each character's face, each location's first state (its
    wide view) and each prop's first state. Every other deliverable is drawn from one
    `parent` image the user picks first: a look from the face, a location state from the
    first state, a plan view from its own state's wide view, a prop state from the first
    state. `root` is the anchor a deliverable is shown under on the board."""

    slots: list[_Slot]
    anchors: list[tuple[str, str]]
    parent: dict[tuple[str, str], tuple[str, str] | None]
    root: dict[tuple[str, str], tuple[str, str] | None]

    def slot(self, key: tuple[str, str]) -> _Slot | None:
        return next((s for s in self.slots if s.key == key), None)


def _graph(pkg: ProductionPackage) -> _Graph:
    slots = _slots(pkg)
    anchors: list[tuple[str, str]] = []
    first: dict[tuple[str, str], tuple[str, str]] = {}  # (kind, entity) -> anchor key
    for slot in slots:
        kind, (entity, _) = slot.req.asset_type, _subject(slot.req)
        if kind == "character_identity":
            anchors.append(slot.key)
            first.setdefault(("character", entity), slot.key)
        elif kind in ("location", "prop") and (kind, entity) not in first:
            if kind == "location" and not slot.deliverable_id.endswith("_wide"):
                continue
            first[(kind, entity)] = slot.key
            anchors.append(slot.key)
    parent: dict[tuple[str, str], tuple[str, str] | None] = {}
    root: dict[tuple[str, str], tuple[str, str] | None] = {}
    anchor_set = set(anchors)
    for slot in slots:
        key = slot.key
        if key in anchor_set:
            parent[key] = root[key] = None
            continue
        kind, (entity, _) = slot.req.asset_type, _subject(slot.req)
        group = "character" if kind.startswith("character") else kind
        anchor = first.get((group, entity))
        root[key] = anchor
        parent[key] = anchor
        if kind == "location" and slot.deliverable_id.endswith("_layout"):
            wide = next(
                (s.key for s in slots
                 if s.req.requirement_id == slot.req.requirement_id
                 and s.deliverable_id.endswith("_wide")),
                None,
            )  # fmt: skip
            parent[key] = wide or anchor
    return _Graph(slots, anchors, parent, root)


def anchor_keys(pkg: ProductionPackage) -> list[tuple[str, str]]:
    """Deliverables everything else is drawn from, in plan order (see _Graph)."""
    return _graph(pkg).anchors


def design_slots(pkg: ProductionPackage) -> list[dict[str, Any]]:
    """Every generated deliverable in plan order, for the design board: what it shows,
    whether it is an anchor, the anchor it is shown under and the image it is drawn from."""
    names = _names(pkg)
    variants = {lk.look_id: lk.description for lk in pkg.story_bible.looks}
    variants |= {s.location_state_id: s.description for s in pkg.story_bible.location_states}
    variants |= {s.prop_state_id: s.description for s in pkg.story_bible.prop_states}
    g = _graph(pkg)
    anchors = set(g.anchors)
    out = []
    for slot in g.slots:
        key = slot.key
        entity, variant = _subject(slot.req)
        root, parent = g.root[key], g.parent[key]
        out.append(
            {
                "requirement_id": key[0],
                "deliverable_id": key[1],
                "asset_type": slot.req.asset_type,
                "view": slot.view,
                "entity_id": entity,
                "entity": names.get(entity, entity),
                "variant": variants.get(variant or "", ""),
                "variant_id": variant,
                "anchor": key in anchors,
                "anchor_key": list(root) if root else None,
                "parent_key": list(parent) if parent else None,
            }
        )
    return out


def _names(pkg: ProductionPackage) -> dict[str, str]:
    names = {c.character_id: c.name for c in pkg.story_bible.characters}
    names |= {loc.location_id: loc.name for loc in pkg.story_bible.locations}
    names |= {p.prop_id: p.name for p in pkg.story_bible.props}
    return names


def slot_title(pkg: ProductionPackage, key: tuple[str, str]) -> str:
    """ "Frank Collins, front face" or "Frank Collins, rain look, full body" for messages."""
    slot = next((s for s in _slots(pkg) if s.key == key), None)
    if slot is None:
        return key[1]
    entity, variant = _subject(slot.req)
    name = _names(pkg).get(entity, entity)
    if slot.req.asset_type == "character_look" and variant:
        # Looks have only an id ("lk_frank_rain"): its words after the character's name.
        words = variant.removeprefix("lk_").split("_")
        first = entity.removeprefix("ch_").split("_")[0]
        label = " ".join(words[1:] if words[:1] == [first] and len(words) > 1 else words)
        return f"{name}, {label} look, {slot.view}"
    return f"{name}, {slot.view}"


class NeedsPick(ValueError):
    """A deliverable is drawn from another image that has no pick yet."""

    def __init__(self, key: tuple[str, str]) -> None:
        super().__init__(f"pick an image for {key[1]} first")
        self.key = key


def selected_image(images: list[GeneratedImage], key: tuple[str, str]) -> GeneratedImage | None:
    """The selected finished image of a deliverable, else its newest finished one."""
    done = [i for i in images if key_of(i) == key and i.status == "done"]
    chosen = [i for i in done if i.selected]
    pool = chosen or done
    return max(pool, key=lambda i: i.created_at) if pool else None
