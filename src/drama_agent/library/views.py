"""Read views of the library: a character or a set with everything it owns, its current
pictures, and the stories and shots that use it."""

from __future__ import annotations

from typing import Any

from ..images import outdated_pictures, selected_image
from .models import LibraryAsset


def _pictures(repo: Any, a: LibraryAsset) -> list[dict[str, Any]]:
    """The asset's current picture per deliverable (the pick, else the newest finished)."""
    links = repo.list_asset_images(asset_id=a.asset_id)
    if not links:
        return []
    by_rev: dict[tuple[str, str], list[Any]] = {}
    stale: dict[tuple[str, str], dict[str, list[str]]] = {}
    for link in links:
        key = (link.package_id, link.revision_id)
        if key not in by_rev:
            by_rev[key] = repo.list_images(*key)
            pkg = repo.get_package(*key)
            # Out of date after a design edit (the same rules as the Assets tab).
            stale[key] = outdated_pictures(pkg, by_rev[key]) if pkg is not None else {}
    out = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        slot = (link.requirement_id, link.deliverable_id)
        if slot in seen:
            continue
        seen.add(slot)
        pick = selected_image(by_rev[(link.package_id, link.revision_id)], slot)
        if pick is None:
            continue
        out.append({
            "image_id": pick.image_id, "view": link.view, "size": pick.size,
            "picked": pick.selected, "likeness": pick.likeness,
            "outdated": stale[(link.package_id, link.revision_id)].get(pick.image_id, []),
            "package_id": link.package_id, "revision_id": link.revision_id,
            "requirement_id": link.requirement_id, "deliverable_id": link.deliverable_id,
            "imported_from": pick.imported_from,
            "content": f"/v1/generated-images/{pick.image_id}/content",
            "thumb": f"/v1/generated-images/{pick.image_id}/thumb",
        })  # fmt: skip
    return out


def _used_in(repo: Any, a: LibraryAsset) -> list[dict[str, Any]]:
    """Each story that casts the asset, with the shots that use it there."""
    out = []
    for c in repo.list_casting(asset_id=a.asset_id):
        pkg = repo.get_package(c.package_id)
        if pkg is None:
            continue
        sid = c.story_id
        shots = [
            s.shot_id for s in pkg.shot_list
            if (a.kind == "character" and sid in s.character_ids)
            or (a.kind == "look" and any(r.look_id == sid for r in s.character_look_refs))
            or (a.kind == "set" and s.location_id == sid)
            or (a.kind == "set_state" and s.location_state_id == sid)
            or (a.kind == "item" and any(r.prop_id == sid for r in s.prop_state_refs))
        ]  # fmt: skip
        out.append({"package_id": pkg.package_id, "title": pkg.story_bible.title,
                    "revision_id": pkg.revision_id, "story_id": sid, "shots": shots})  # fmt: skip
    return out


def _entry(repo: Any, a: LibraryAsset, *, full: bool) -> dict[str, Any]:
    data = a.model_dump(mode="json")
    data["pictures"] = _pictures(repo, a)
    # The story revision its edits go to (a look of a linked story goes to that story).
    story = repo.get_package(a.origin_package_id)
    data["origin_revision_id"] = story.revision_id if story is not None else None
    if full:
        data["used_in"] = _used_in(repo, a)
    return data


def asset_view(repo: Any, a: LibraryAsset) -> dict[str, Any]:
    """A character or a set with its looks or states and its items, each with its pictures,
    and where each is used. `origin_revision_id` is the story revision its edits go to."""
    out = _entry(repo, a, full=True)
    story = repo.get_package(a.origin_package_id)
    out["origin_revision_id"] = story.revision_id if story is not None else None
    child = "look" if a.kind == "character" else "set_state"
    # The copies other stories linked to this one: their own pictures, shown beside its own.
    out["also"] = [
        {"package_id": m.origin_package_id, "story_id": m.origin_entity_id,
         "title": (p.story_bible.title if (p := repo.get_package(m.origin_package_id)) else ""),
         "pictures": _pictures(repo, m)}
        for m in repo.list_library_assets(kind=a.kind) if m.merged_into == a.asset_id
    ]  # fmt: skip
    out["looks" if a.kind == "character" else "states"] = [
        _entry(repo, x, full=True) for x in repo.list_library_assets(kind=child, parent_id=a.asset_id)
    ]  # fmt: skip
    out["items"] = [_entry(repo, x, full=True)
                    for x in repo.list_library_assets(kind="item", parent_id=a.asset_id)]  # fmt: skip
    return out


def asset_list(repo: Any, kind: str) -> list[dict[str, Any]]:
    """Every character (or set) in name order, with its pictures and how much it owns."""
    child = "look" if kind == "character" else "set_state"
    out = []
    shown = [a for a in repo.list_library_assets(kind=kind)
             if a.merged_into is None and not a.archived]  # fmt: skip
    for a in sorted(shown, key=lambda x: x.name.lower()):
        entry = _entry(repo, a, full=False)
        children = repo.list_library_assets(kind=child, parent_id=a.asset_id)
        entry["children"] = len(children)
        # A cover: its own picture (a character's face), else its first look's or state's.
        covers = entry["pictures"] or next((p for c in children if (p := _pictures(repo, c))), [])
        entry["cover"] = covers[0] if covers else None
        entry["items"] = len(repo.list_library_assets(kind="item", parent_id=a.asset_id))
        entry["stories"] = len(repo.list_casting(asset_id=a.asset_id))
        out.append(entry)
    return out
