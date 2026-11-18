"""Reuse a shared asset's picture in a story linked to it (phase 3 of
docs/entity-reorganization.md).

A story whose character (or set) is linked to another story's asset can take that asset's
current pick for its own anchor: the face of a character, the main wide view of a set (its
first state). The picture is copied into the story as an imported pick, like an Experiment
image. Everything the story drew from its old anchor (a character's looks, a set's other
states and plan views) is then out of date, and "Redraw out-of-date" redraws it from the
shared picture. The story's old pictures stay among its takes, so the change can be undone
by picking one again.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..images import _graph, _subject, import_pick, make_thumb, outdated_pictures, selected_image
from ..models import ProductionPackage
from ..models.images import GeneratedImage


class ReuseError(ValueError):
    """The picture can't be reused (says why)."""


def anchor_key(pkg: ProductionPackage, kind: str, entity_id: str) -> tuple[str, str] | None:
    """The anchor deliverable of a story's character (its face) or set (its first state's
    wide view)."""
    g = _graph(pkg)
    for key in g.anchors:
        slot = g.slot(key)
        if slot is None:
            continue
        entity, _ = _subject(slot.req)
        if entity != entity_id:
            continue
        if kind == "character" and slot.req.asset_type == "character_identity":
            return key
        if kind == "set" and slot.req.asset_type == "location":
            return key
    return None


@dataclass
class Reused:
    image: GeneratedImage
    source_image_id: str
    outdated: int  # the story's pictures now out of date (to redraw from it)


def reuse_anchor(repo: Any, media: Any, package_id: str, story_id: str) -> Reused:
    """Copy the linked asset's current anchor pick into the story as its pick."""
    row = next((c for c in repo.list_casting(package_id=package_id) if c.story_id == story_id),
               None)  # fmt: skip
    if row is None or row.source != "user" or row.kind not in ("character", "set"):
        raise ReuseError(f"{story_id} is not linked to another story's character or set")
    shared = repo.get_library_asset(row.asset_id)
    src_pkg = shared and repo.get_package(shared.origin_package_id)
    dst_pkg = repo.get_package(package_id)
    if shared is None or src_pkg is None or dst_pkg is None:
        raise ReuseError("the linked asset or its story is gone")
    what = "face" if row.kind == "character" else "main set view"
    src_key = anchor_key(src_pkg, row.kind, shared.origin_entity_id)
    dst_key = anchor_key(dst_pkg, row.kind, story_id)
    if src_key is None or dst_key is None:
        raise ReuseError(f"no {what} to reuse here")
    src = selected_image(repo.list_images(src_pkg.package_id, src_pkg.revision_id), src_key)
    if src is None or not src.file:
        raise ReuseError(f"{shared.name} has no picked {what} yet")
    ext = PurePosixPath(src.file).suffix or ".jpg"
    rel = f"generated/{dst_pkg.package_id}/reuse_{src.image_id}{ext}"
    dest = media.path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(media.path(src.file), dest)
    jobs = repo.jobs_for_package(package_id)
    img = import_pick(
        dst_pkg, dst_key, file=rel, thumb=make_thumb(media, rel), content_type=src.content_type,
        prompt=src.prompt, likeness=src.likeness,
        imported_from=f"library {shared.asset_id} ({src_pkg.story_bible.title}), {src.image_id}",
        job_id=jobs[0].job_id if jobs else None,
    )  # fmt: skip
    repo.save_image(img)
    repo.set_selected(img.image_id)
    images = repo.list_images(dst_pkg.package_id, dst_pkg.revision_id)
    return Reused(image=img, source_image_id=src.image_id,
                  outdated=len(outdated_pictures(dst_pkg, images)))  # fmt: skip
