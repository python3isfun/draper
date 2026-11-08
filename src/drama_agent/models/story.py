"""Shared story and asset settings (spec section 6A).

These are reusable text descriptions only; they never represent generated images.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Character(BaseModel):
    character_id: str
    name: str
    appearance: str
    costume: str
    relationships: dict[str, str] = Field(
        default_factory=dict,
        description="Keys: other existing character IDs only. Values: the relationship, as prose.",
    )
    goal: str
    motivation: str
    knowledge: list[str] = Field(default_factory=list)


class CharacterLook(BaseModel):
    look_id: str
    character_id: str
    description: str
    diff_from_base: str = ""
    applies_to_scene_ids: list[str] = Field(default_factory=list)


class Location(BaseModel):
    location_id: str
    name: str
    layout: str
    furnishings: str
    lighting: str
    entrances: str


class LocationState(BaseModel):
    location_state_id: str
    location_id: str
    description: str
    diff_from_base: str = ""
    applies_to_scene_ids: list[str] = Field(default_factory=list)


class Prop(BaseModel):
    """Only props that affect plot or continuity are registered."""

    prop_id: str
    name: str
    description: str
    owner_character_id: str | None = None


class PropState(BaseModel):
    prop_state_id: str
    prop_id: str
    description: str
    diff_from_base: str = ""
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
