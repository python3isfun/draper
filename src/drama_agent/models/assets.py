"""Art asset requirements: the downstream design hand-off contract (spec section 6D).

A requirement is a design need, never a generated image. Requirement IDs, entity IDs and
real image asset_ids are kept separate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AssetType = Literal["character_identity", "character_look", "location", "prop", "shot_reference"]
EntityType = Literal["character", "location", "prop"]
RequirementStatus = Literal["draft", "ready", "stale"]
Priority = Literal["required", "optional"]

ASSET_REQUIREMENTS_SCHEMA_VERSION = "1.0"


class SubjectRef(BaseModel):
    entity_type: EntityType
    entity_id: str
    # look_id / location_state_id / prop_state_id; None for a base identity.
    variant_id: str | None = None


class SourceRef(BaseModel):
    asset_id: str
    start_ms: int | None = None
    end_ms: int | None = None


class UsedBy(BaseModel):
    episode_id: str
    scene_id: str
    shot_id: str


class Deliverable(BaseModel):
    deliverable_id: str
    view: str
    purpose: str
    required: bool = True
    aspect_ratio: str | None = None


class Fulfillment(BaseModel):
    """Back-filled by the downstream design service."""

    deliverable_id: str
    asset_id: str
    asset_version: str


class AssetRequirement(BaseModel):
    requirement_id: str
    asset_type: AssetType
    subject_refs: list[SubjectRef] = Field(min_length=1)
    design_brief: str
    must_keep: list[str] = Field(default_factory=list)
    design_freedom: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    used_by: list[UsedBy] = Field(min_length=1)
    deliverables: list[Deliverable] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: Priority = "required"
    requirement_status: RequirementStatus = "draft"
    fulfilled_by: list[Fulfillment] = Field(default_factory=list)


class StyleBrief(BaseModel):
    medium: str  # e.g. live-action realistic, animation
    era_region: str
    color_and_light: str
    aspect_ratio: str
    references: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class AssetRequirements(BaseModel):
    schema_version: str = ASSET_REQUIREMENTS_SCHEMA_VERSION
    style_brief: StyleBrief
    items: list[AssetRequirement] = Field(default_factory=list)

    def by_id(self) -> dict[str, AssetRequirement]:
        return {r.requirement_id: r for r in self.items}
