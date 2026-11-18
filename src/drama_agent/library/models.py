"""The asset library: characters, sets and the items they own, shared across stories.

Phase 1 of docs/entity-reorganization.md: the library mirrors each story's latest revision
(its cast, set and prop sheets and the story bible's one-line descriptions). Each story
entity is its own asset (no matching across stories yet); `origin_*` says where it came
from, and `casting` maps the story's ids to the library's. Pictures stay where they are
(`generated_images`); `AssetImage` links each to the asset it shows.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models.package import utcnow

# character: a person; look: one of their looks; set: a place; set_state: one of its states;
# item: a prop, owned by the character who carries it or the set it dresses (or no one).
AssetKind = Literal["character", "look", "set", "set_state", "item"]
PREFIX: dict[str, str] = {
    "character": "chr", "look": "look", "set": "set", "set_state": "state", "item": "item",
}  # fmt: skip


def asset_id(kind: AssetKind, package_id: str, entity_id: str) -> str:
    """A stable id for a story entity's asset: syncing again finds the same one."""
    digest = hashlib.sha256(f"{package_id}|{entity_id}".encode()).hexdigest()[:12]
    return f"{PREFIX[kind]}_{digest}"


class LibraryAsset(BaseModel):
    asset_id: str
    kind: AssetKind
    # look -> its character; set_state -> its set; item -> its owner (character or set), or None.
    parent_id: str | None = None
    name: str
    # The story bible's one-line description (a look, a state, a prop's story function).
    description: str = ""
    # The design sheet entry (CastMember, CastLook, SetLocation, SetState, PropItem), as data.
    sheet: dict[str, Any] = Field(default_factory=dict)
    # An item's states (prop states), each {state_id, description, sheet}.
    states: list[dict[str, Any]] = Field(default_factory=list)
    # Where it came from: the story and its id there.
    origin_package_id: str
    origin_entity_id: str
    # A fingerprint of the story text it mirrors: a sync rewrites it only when that changed.
    source_print: str = ""
    # A story's own copy of a character or set the user linked to another story's (phase 3):
    # hidden from the lists; its looks or states join that asset.
    merged_into: str | None = None
    # From a story whose jobs are all archived: kept, not shown (like the story itself).
    archived: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Casting(BaseModel):
    """A story entity (character, look, location, state or prop id) cast as a library asset."""

    package_id: str
    story_id: str
    asset_id: str
    kind: AssetKind
    # "story": the story's own copy (synced); "user": linked by a person to another story's
    # asset (kept by every sync until unlinked).
    source: Literal["story", "user"] = "story"


class AssetImage(BaseModel):
    """A picture of a library asset: a generated image (kept where it is), its owner and view."""

    image_id: str
    asset_id: str
    package_id: str
    revision_id: str
    requirement_id: str
    deliverable_id: str
    view: str = ""
