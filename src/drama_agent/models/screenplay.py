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
    # Descriptions reach the model through the JSON schema; keys are checked by
    # check_entity_keys, values are prose (checked for ID/timecode leaks).
    knowledge: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Keys: existing character IDs only. Values: facts that character knows, "
        "as prose.",
    )
    possessions: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Keys: existing character IDs only. Values: what that character holds "
        "and where, as prose, using names rather than IDs.",
    )
    relationships: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# Ordered from least to most intimate; a job's content rating caps the level.
IntimacyLevel = Literal["none", "kiss", "implied", "simulated"]
INTIMACY_ORDER: tuple[IntimacyLevel, ...] = ("none", "kiss", "implied", "simulated")


class Intimacy(BaseModel):
    """How a scene stages physical intimacy, for the director and the intimacy coordinator.
    Written as staging, never as explicit description."""

    level: IntimacyLevel = Field(
        "none",
        description=(
            "none; kiss (a kiss or embrace); implied (the scene cuts away or starts after: "
            "a door closing, the morning after); simulated (simulated sex is on screen)."
        ),
    )
    character_ids: list[str] = Field(
        default_factory=list, description="The characters in the intimate moment."
    )
    on_screen: str = Field(
        "",
        description="What the camera shows, as staging (e.g. a kiss, then a cut to the hallway).",
    )
    off_screen: str = Field("", description="What is implied and not shown.")
    nudity: Literal["none", "partial"] = Field(
        "none", description="partial: bare back or shoulders; never full nudity."
    )
    consent_beats: list[str] = Field(
        default_factory=list,
        description="Moments in the scene where consent is clear on screen (a look, a line).",
    )
    closed_set: bool = Field(False, description="True when only essential crew should be present.")


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
    # None (older packages, and scenes the writer left out) means no intimacy.
    intimacy: Intimacy | None = None

    @property
    def intimacy_level(self) -> IntimacyLevel:
        return self.intimacy.level if self.intimacy else "none"

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
