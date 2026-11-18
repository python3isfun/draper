"""Mirror stories into the library (phase 1 of docs/entity-reorganization.md).

`build` turns one story's latest revision into library assets, its casting and picture
links: pure, so it's testable on its own. `sync_library` runs it for every story and saves
what changed. It is idempotent: asset ids come from the story and its ids, an asset is
rewritten only when the story text it mirrors changed, and links are upserted.

Props are split by use: one with an owner (the story bible's `owner_character_id`) is an
item of that character; otherwise an item of the set it appears in most (by the shots that
use it); otherwise it has no owner yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.package import utcnow
from .models import AssetImage, AssetKind, Casting, LibraryAsset, asset_id

log = logging.getLogger(__name__)

# Asset requirement type -> the kind of asset its pictures show, and which ref id names it.
_OWNERS: dict[str, tuple[AssetKind, str]] = {
    "character_identity": ("character", "entity"),
    "character_look": ("look", "variant"),
    "location": ("set_state", "variant"),
    "prop": ("item", "entity"),
}


@dataclass
class Built:
    assets: list[LibraryAsset] = field(default_factory=list)
    casting: list[Casting] = field(default_factory=list)
    images: list[AssetImage] = field(default_factory=list)


def _print(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _dump(obj: Any) -> dict[str, Any]:
    return obj.model_dump(mode="json") if obj is not None else {}


def build(pkg: ProductionPackage, images: list[GeneratedImage]) -> Built:
    """The library assets, casting and picture links of one story revision."""
    out = Built()
    pid = pkg.package_id
    bible = pkg.story_bible
    cast = {c.character_id: c for c in (pkg.cast_sheet.characters if pkg.cast_sheet else [])}
    cast_looks = {x.look_id: x for x in (pkg.cast_sheet.looks if pkg.cast_sheet else [])}
    locations = {x.location_id: x for x in (pkg.set_sheet.locations if pkg.set_sheet else [])}
    set_states = {x.location_state_id: x for x in (pkg.set_sheet.states if pkg.set_sheet else [])}
    props = {x.prop_id: x for x in (pkg.prop_sheet.props if pkg.prop_sheet else [])}
    prop_states = {x.prop_state_id: x for x in (pkg.prop_sheet.states if pkg.prop_sheet else [])}

    def add(kind: AssetKind, entity_id: str, name: str, parent: str | None, *,
            description: str = "", sheet: dict[str, Any] | None = None,
            states: list[dict[str, Any]] | None = None) -> str:  # fmt: skip
        aid = asset_id(kind, pid, entity_id)
        sheet = sheet or {}
        out.assets.append(LibraryAsset(
            asset_id=aid, kind=kind, parent_id=parent, name=name, description=description,
            sheet=sheet, states=states or [], origin_package_id=pid, origin_entity_id=entity_id,
            source_print=_print(kind, name, parent, description, sheet, states or []),
        ))  # fmt: skip
        out.casting.append(Casting(package_id=pid, story_id=entity_id, asset_id=aid, kind=kind))
        return aid

    for c in bible.characters:
        chr_id = add("character", c.character_id, c.name, None, description=c.role,
                     sheet=_dump(cast.get(c.character_id)))  # fmt: skip
        for lk in bible.looks:
            if lk.character_id == c.character_id:
                add("look", lk.look_id, f"{c.name}: {lk.look_id}", chr_id,
                    description=lk.description, sheet=_dump(cast_looks.get(lk.look_id)))  # fmt: skip
    for loc in bible.locations:
        set_id = add("set", loc.location_id, loc.name, None, description=loc.summary,
                     sheet=_dump(locations.get(loc.location_id)))  # fmt: skip
        for st in bible.location_states:
            if st.location_id == loc.location_id:
                add("set_state", st.location_state_id, f"{loc.name}: {st.location_state_id}",
                    set_id, description=st.description,
                    sheet=_dump(set_states.get(st.location_state_id)))  # fmt: skip

    # Props, split by use: carried by their owner, else dressing the set they appear in most.
    where: dict[str, Counter[str]] = {}
    for shot in pkg.shot_list:
        for ref in shot.prop_state_refs:
            where.setdefault(ref.prop_id, Counter())[shot.location_id] += 1
    for p in bible.props:
        if p.owner_character_id:
            owner: str | None = asset_id("character", pid, p.owner_character_id)
        elif p.prop_id in where:
            owner = asset_id("set", pid, where[p.prop_id].most_common(1)[0][0])
        else:
            owner = None
        states = [{"state_id": s.prop_state_id, "description": s.description,
                   "sheet": _dump(prop_states.get(s.prop_state_id))}
                  for s in bible.prop_states if s.prop_id == p.prop_id]  # fmt: skip
        add("item", p.prop_id, p.name, owner, description=p.story_function,
            sheet=_dump(props.get(p.prop_id)), states=states)  # fmt: skip

    # Every picture of this revision, linked to the asset it shows (a prop state's picture
    # to its item). Story-level shot references are not assets.
    reqs = {r.requirement_id: r for r in pkg.asset_requirements.items}
    known = {a.asset_id for a in out.assets}
    for img in images:
        req = reqs.get(img.requirement_id)
        if req is None or req.asset_type not in _OWNERS or not req.subject_refs:
            continue
        kind, which = _OWNERS[req.asset_type]
        ref = req.subject_refs[0]
        story_id = ref.variant_id if which == "variant" and ref.variant_id else ref.entity_id
        if kind == "set_state" and not ref.variant_id:
            kind = "set"
        aid = asset_id(kind, pid, story_id)
        if aid not in known:
            continue
        view = next((d.view for d in req.deliverables if d.deliverable_id == img.deliverable_id),
                    "")  # fmt: skip
        out.images.append(AssetImage(
            image_id=img.image_id, asset_id=aid, package_id=pid, revision_id=img.revision_id,
            requirement_id=img.requirement_id, deliverable_id=img.deliverable_id, view=view,
        ))  # fmt: skip
    return out


def apply_links(made: Built, linked: dict[str, Casting]) -> None:
    """A linked character or set: its own copy is marked merged into the shared asset, and
    its looks or states (and items) hang off the shared asset instead, as more of it."""
    into = {}
    for a in made.assets:
        if a.kind in ("character", "set") and a.origin_entity_id in linked:
            into[a.asset_id] = linked[a.origin_entity_id].asset_id
            a.merged_into = into[a.asset_id]
    for a in made.assets:
        if a.parent_id in into:
            a.parent_id = into[a.parent_id]
        if a.asset_id in into or a.parent_id in into.values():
            a.source_print = _print(a.source_print, a.parent_id, a.merged_into)


@dataclass
class SyncReport:
    stories: int = 0
    added: int = 0
    updated: int = 0
    pictures: int = 0


# What each story looked like when last synced (per repository): a story whose text and
# pictures haven't changed since is skipped, so a sync before every read stays cheap.
_SYNCED: dict[tuple[int, str], str] = {}


def sync_library(repo: Any, *, force: bool = False) -> SyncReport:
    """Mirror every story's latest revision into the library. Safe to run any time; a story
    unchanged since its last sync is skipped unless `force`."""
    report = SyncReport()
    for pid in repo.list_package_ids():
        pkg = repo.get_package(pid)
        if pkg is None:
            continue
        report.stories += 1
        jobs = repo.jobs_for_package(pid)
        archived = bool(jobs) and all(j.archived for j in jobs)
        images = repo.list_images(pkg.package_id, pkg.revision_id)
        # Links a person made (phase 3): this story's character or set cast as another's.
        linked = {c.story_id: c for c in repo.list_casting(package_id=pid) if c.source == "user"}
        # Its text changes with a new revision or a hand edit (each logged); its links with
        # the set of pictures and of linked entities.
        state = _print(pkg.revision_id, len(pkg.change_log), sorted(i.image_id for i in images),
                       sorted((k, c.asset_id) for k, c in linked.items()), archived)  # fmt: skip
        if not force and _SYNCED.get((id(repo), pid)) == state:
            continue
        made = build(pkg, images)
        apply_links(made, linked)
        for a in made.assets:
            a.archived = archived
            old = repo.get_library_asset(a.asset_id)
            if old is None:
                repo.save_library_asset(a)
                report.added += 1
            elif (old.source_print, old.archived) != (a.source_print, a.archived):
                repo.save_library_asset(a.model_copy(update={"created_at": old.created_at,
                                                             "updated_at": utcnow()}))  # fmt: skip
                report.updated += 1
        for c in made.casting:
            if c.story_id not in linked:  # a person's link is kept
                repo.save_casting(c)
        for link in made.images:
            repo.link_asset_image(link)
        report.pictures += len(made.images)
        _SYNCED[(id(repo), pid)] = state
    return report
