"""Shared story settings (spec section 6A): the story-facing side of each entity.

How characters, locations and props look lives in the design sheets
(models/design_sheets.py), keyed by the same IDs. Fields removed from here when the sheets
were introduced (appearance, costume, layout, ...) move into `legacy` when old packages
load.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema


class _KeepsLegacy(BaseModel):
    """Keeps the visual fields of packages made before the design sheets.

    They move into `legacy` on load instead of being dropped, so old packages still show
    their descriptions. `legacy` is hidden from the model's schema and left out of the JSON
    when empty, so new packages never carry it.
    """

    _legacy_fields: ClassVar[tuple[str, ...]] = ()
    legacy: SkipJsonSchema[dict[str, str] | None] = Field(default=None, exclude_if=lambda v: not v)

    @model_validator(mode="before")
    @classmethod
    def _move_legacy(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(k in data for k in cls._legacy_fields):
            data = dict(data)
            old = {k: str(data.pop(k)) for k in cls._legacy_fields if k in data}
            old = {k: v for k, v in old.items() if v and v != "None"}
            if old:
                data["legacy"] = {**(data.get("legacy") or {}), **old}
        return data


class Character(_KeepsLegacy):
    _legacy_fields = ("appearance", "costume")
    character_id: str
    name: str
    role: str = Field("", description="Story role, e.g. protagonist, antagonist, mentor.")
    relationships: dict[str, str] = Field(
        default_factory=dict,
        description="Keys: other existing character IDs only. Values: the relationship, as prose.",
    )
    goal: str
    motivation: str
    knowledge: list[str] = Field(default_factory=list)


_VARIANT = (
    "One line, story-facing: what this variant is and why it exists. Visual detail goes "
    "in the design sheets."
)


class CharacterLook(_KeepsLegacy):
    _legacy_fields = ("diff_from_base",)
    look_id: str
    character_id: str
    description: str = Field(description=_VARIANT)
    applies_to_scene_ids: list[str] = Field(default_factory=list)


class Location(_KeepsLegacy):
    _legacy_fields = ("layout", "furnishings", "lighting", "entrances")
    location_id: str
    name: str
    summary: str = Field("", description="What this place is in the story, in one line.")


class LocationState(_KeepsLegacy):
    _legacy_fields = ("diff_from_base",)
    location_state_id: str
    location_id: str
    description: str = Field(description=_VARIANT)
    applies_to_scene_ids: list[str] = Field(default_factory=list)


class Prop(_KeepsLegacy):
    """Only props that affect plot or continuity are registered."""

    _legacy_fields = ("description",)
    prop_id: str
    name: str
    story_function: str = Field("", description="Why this prop matters to the plot.")
    owner_character_id: str | None = None


class PropState(_KeepsLegacy):
    _legacy_fields = ("diff_from_base",)
    prop_state_id: str
    prop_id: str
    description: str = Field(description=_VARIANT)
    applies_to_scene_ids: list[str] = Field(default_factory=list)


class BeatOutline(BaseModel):
    episode_id: str
    opening: str
    protagonist_action: str
    cost_or_reversal: str
    ending_change: str
    cliffhanger_setup_refs: list[str] = Field(
        default_factory=list,
        description="IDs of previously planted characters/props/facts the ending relies on.",
    )


class StoryBible(BaseModel):
    title: str
    logline: str
    genre: str
    audience_assumption: str
    emotional_goal: str
    core_conflict: str
    series_synopsis: str
    # Required when story_scope == series_opening.
    future_direction: str | None = None
    character_arcs: dict[str, str] = Field(
        default_factory=dict,
        description="Keys: existing character IDs only. Values: that character's arc, as prose.",
    )
    characters: list[Character]
    looks: list[CharacterLook] = Field(default_factory=list)
    locations: list[Location]
    location_states: list[LocationState] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
    prop_states: list[PropState] = Field(default_factory=list)
    beat_outlines: list[BeatOutline] = Field(default_factory=list)
