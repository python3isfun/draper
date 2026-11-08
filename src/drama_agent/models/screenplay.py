"""Three-episode screenplay (spec section 6B)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DialogueLine(BaseModel):
    dialogue_id: str
    character_id: str
    text: str
    delivery: str = ""
    # Voice-only lines (phone / off-screen) still count the speaker as a narrative character.
    voice_only: bool = False


class Beat(BaseModel):
    """One action/dialogue/sound unit. Shots reference beats via source_beat_ids."""

    beat_id: str
    action: str | None = None
    dialogue: DialogueLine | None = None
    sound: str | None = None


class SceneState(BaseModel):
    knowledge: dict[str, list[str]] = Field(default_factory=dict)  # character_id -> facts
    possessions: dict[str, list[str]] = Field(default_factory=dict)  # character_id -> prop_ids
    relationships: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Scene(BaseModel):
    scene_id: str
    number: int
    int_ext: Literal["INT", "EXT"]
    location_id: str
    location_state_id: str
    time_of_day: str
    # All narrative characters in the scene, including voice-only ones.
    character_ids: list[str]
    target_duration_ms: int = Field(gt=0)
    beats: list[Beat]
    start_state: SceneState = Field(default_factory=SceneState)
    end_state: SceneState = Field(default_factory=SceneState)
    new_information: list[str] = Field(default_factory=list)

    def dialogue_lines(self) -> list[DialogueLine]:
        return [b.dialogue for b in self.beats if b.dialogue is not None]


class Episode(BaseModel):
    episode_id: str
    number: int
    title: str
    opening_event: str
    main_conflict: str
    ending_event: str
    scenes: list[Scene]
    # Carried into the next episode: who knows what, who holds what, open questions.
    end_state: SceneState = Field(default_factory=SceneState)

    def character_ids(self) -> set[str]:
        return {cid for s in self.scenes for cid in s.character_ids}
