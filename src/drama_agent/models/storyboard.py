"""Text storyboard / shot list (spec section 6C) and timing (spec section 7)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CharacterLookRef(BaseModel):
    character_id: str
    look_id: str


class PropStateRef(BaseModel):
    prop_id: str
    prop_state_id: str


class DialogueCue(BaseModel):
    """The slice of an episode-level scheduled line that plays during this shot."""

    dialogue_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class Shot(BaseModel):
    """An edit shot. Not the same thing as one video-model generation call."""

    shot_id: str
    episode_id: str
    scene_id: str
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    # Characters visible on screen. Voice-only characters appear only via dialogue cues.
    character_ids: list[str] = Field(default_factory=list)
    location_id: str
    character_look_refs: list[CharacterLookRef] = Field(default_factory=list)
    location_state_id: str
    prop_state_refs: list[PropStateRef] = Field(default_factory=list)
    asset_requirement_ids: list[str] = Field(default_factory=list)
    framing: str
    camera: str
    visual_action: str
    dialogue_cues: list[DialogueCue] = Field(default_factory=list)
    sound: str = ""
    continuity: str = ""
    narrative_function: str
    source_beat_ids: list[str] = Field(min_length=1)
    generation_notes: str = ""
    # Set by the storyboard step when a composition reference image is needed.
    needs_shot_reference: bool = False

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


class DialogueScheduleEntry(BaseModel):
    """Each dialogue_id is scheduled exactly once on the episode audio track."""

    dialogue_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    overlap_allowed: bool = False  # explicit interruption / overlapping speech


class EpisodeTiming(BaseModel):
    episode_id: str
    dialogue_schedule: list[DialogueScheduleEntry] = Field(default_factory=list)
    speech_rate_assumption: str = ""
    planned_timing_valid: bool = False
    # Must stay False until a read-through / voice track has been measured.
    spoken_timing_verified: bool = False
