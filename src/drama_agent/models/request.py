"""Input contract for a drama job (spec section 2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SourceType = Literal["text", "image", "video", "script"]
SourceUse = Literal[
    "story_seed",
    "character_reference",
    "location_reference",
    "visual_style",
    "plot_reference",
]
StoryScope = Literal["series_opening", "complete_short"]
ReferenceMode = Literal["inspiration", "adaptation"]


class Source(BaseModel):
    """One creative input. Media is referenced by service asset_id."""

    type: SourceType
    text: str | None = None
    asset_id: str | None = None
    use_as: SourceUse | None = None
    # Adaptation only: elements the user requires to be kept / allowed to change.
    must_keep: list[str] = Field(default_factory=list)
    may_change: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_payload(self) -> Source:
        if self.type == "text" and not self.text:
            raise ValueError("text source requires 'text'")
        if self.type in ("image", "video") and not self.asset_id:
            raise ValueError(f"{self.type} source requires 'asset_id'")
        if self.type == "script" and not (self.text or self.asset_id):
            raise ValueError("script source requires 'text' or 'asset_id'")
        return self


class IntRange(BaseModel):
    min: int
    max: int

    @model_validator(mode="after")
    def _ordered(self) -> IntRange:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class JobOptions(BaseModel):
    # None means: inherit from text input; visual-only input defaults to zh-CN (recorded as
    # an assumption in the source brief).
    language: str | None = None
    aspect_ratio: str = "9:16"
    story_scope: StoryScope = "series_opening"
    episode_count: Literal[3] = 3
    episode_duration_seconds: Literal[120] = 120
    scenes_per_episode: IntRange = Field(default_factory=lambda: IntRange(min=2, max=3))
    characters_per_episode: IntRange = Field(default_factory=lambda: IntRange(min=3, max=5))
    genre: str | None = None
    audience: str | None = None
    reference_mode: ReferenceMode = "inspiration"
    variants: int = Field(default=1, ge=1)
    # Automatic revision attempts per stage (spec section 8 suggests 2).
    revision_budget_per_stage: int = Field(default=2, ge=0, le=5)

    @property
    def episode_duration_ms(self) -> int:
        return self.episode_duration_seconds * 1000


class JobRequest(BaseModel):
    sources: list[Source] = Field(min_length=1)
    options: JobOptions = Field(default_factory=JobOptions)
