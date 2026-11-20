"""Input contract for a drama job (spec section 2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..pace import DEFAULT_PACE, Pace, pace_name

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
# A story's rating, as the MPA rates American films; it caps the story's intimate staging
# (RATING_LIMITS) and what its images may show (content_guard.py). NC-17 is not offered.
ContentRating = Literal["G", "PG", "PG-13", "R"]
RATINGS: tuple[ContentRating, ...] = ("G", "PG", "PG-13", "R")
DEFAULT_RATING: ContentRating = "PG-13"
# The ratings jobs were stored with before the MPA ratings.
LEGACY_RATINGS: dict[str, ContentRating] = {"general": "PG", "teen": "PG-13", "mature": "R"}
# rating -> (highest scene intimacy level, partial nudity allowed)
RATING_LIMITS: dict[str, tuple[str, bool]] = {
    "G": ("kiss", False),
    "PG": ("kiss", False),
    "PG-13": ("implied", False),
    "R": ("simulated", True),
}


def _known_rating(value: object) -> ContentRating | None:
    """An MPA rating in any case ("pg-13", "r") or a legacy one (general/teen/mature)."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    mpa = {r.upper(): r for r in RATINGS}.get(name.upper())
    return mpa or LEGACY_RATINGS.get(name.lower())


def as_rating(value: object) -> ContentRating:
    """A rating, from an MPA rating or a legacy one (general/teen/mature); else the default."""
    return _known_rating(value) or DEFAULT_RATING


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
    # Stories are written in American English and set in the present-day US: en-US is the
    # only output language the API accepts. (Jobs stored earlier may carry another value.)
    language: str | None = "en-US"
    aspect_ratio: str = "9:16"
    story_scope: StoryScope = "series_opening"
    episode_count: Literal[3] = 3
    episode_duration_seconds: Literal[120] = 120
    scenes_per_episode: IntRange = Field(default_factory=lambda: IntRange(min=2, max=3))
    characters_per_episode: IntRange = Field(default_factory=lambda: IntRange(min=3, max=5))
    genre: str | None = None
    audience: str | None = None
    reference_mode: ReferenceMode = "inspiration"
    # G/PG: a kiss at most; PG-13: intimacy implied (cut away), no nudity; R: simulated
    # intimacy and partial nudity, staged for a closed set. Images follow it too.
    content_rating: ContentRating = DEFAULT_RATING
    # How the storyboard and cut plans keep the clips moving (pace.py); jobs stored before
    # it had none and are brisk.
    pace: Pace = DEFAULT_PACE
    variants: int = Field(default=1, ge=1)
    # Automatic revision attempts per stage (spec section 8 suggests 2).
    revision_budget_per_stage: int = Field(default=2, ge=0, le=5)

    @field_validator("content_rating", mode="before")
    @classmethod
    def _mpa_rating(cls, value: object) -> object:
        """Jobs stored before the MPA ratings say general/teen/mature; any case is read. An
        unknown rating stays as it is, so it is refused."""
        return _known_rating(value) or value

    @field_validator("pace", mode="before")
    @classmethod
    def _pace(cls, value: object) -> object:
        """A pace in any case or spelling ("Slow burn"); none is the default; an unknown
        one stays as it is, so it is refused."""
        if value is None:
            return DEFAULT_PACE
        return pace_name(value) if isinstance(value, str) else value

    @property
    def episode_duration_ms(self) -> int:
        return self.episode_duration_seconds * 1000


class JobRequest(BaseModel):
    sources: list[Source] = Field(min_length=1)
    options: JobOptions = Field(default_factory=JobOptions)
