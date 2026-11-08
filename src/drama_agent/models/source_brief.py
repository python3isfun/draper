"""Normalized understanding of the inputs (spec section 3)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TimeRange(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class SourceCoverage(BaseModel):
    """What was actually analyzed. Unanalyzed parts must be declared, never assumed."""

    source_index: int
    asset_id: str | None = None
    analyzed_ranges: list[TimeRange] = Field(default_factory=list)
    unknown_ranges: list[TimeRange] = Field(default_factory=list)
    analyzed_sections: list[str] = Field(default_factory=list)
    notes: str = ""


class CreativeBrief(BaseModel):
    genre: str
    audience_assumption: str
    core_conflict: str
    emotional_experience: str
    reusable_assets: list[str] = Field(default_factory=list)


class SourceBrief(BaseModel):
    language: str
    observed_facts: list[str] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    creative_assumptions: list[str] = Field(default_factory=list)
    source_coverage: list[SourceCoverage] = Field(min_length=1)
    creative_brief: CreativeBrief
