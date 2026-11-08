"""Uploaded source media (spec sections 2-3, 9: source_assets).

Files live in the MediaStore under `<asset_id>/`; this record holds what was extracted from
them. Paths are relative to the store root so the store can move.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .package import utcnow

MediaKind = Literal["image", "video"]
TranscriptStatus = Literal["pending", "done", "no_audio", "failed"]


class VideoFrame(BaseModel):
    t_ms: int = Field(ge=0)
    file: str  # JPEG, relative to the store root


class TranscriptSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class SourceAsset(BaseModel):
    asset_id: str
    kind: MediaKind
    filename: str  # as uploaded; display only, never used as a path
    content_type: str  # sniffed from the bytes, not taken from the client
    size_bytes: int
    sha256: str
    created_at: datetime = Field(default_factory=utcnow)
    file: str  # the original, relative to the store root
    width: int | None = None
    height: int | None = None
    # Image: a downscaled JPEG sent to the model (falls back to the original).
    model_image: str | None = None
    # Video only.
    duration_ms: int | None = None
    has_audio: bool = False
    frames: list[VideoFrame] = Field(default_factory=list)
    transcript_status: TranscriptStatus | None = None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    # Audio after this point was not transcribed (length cap); None means all of it.
    transcript_until_ms: int | None = None
    transcript_model: str | None = None
    transcript_error: str | None = None
