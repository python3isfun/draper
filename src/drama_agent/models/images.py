"""Reference images generated for asset requirements (on demand, never part of a job).

A package revision is immutable, so images are not written into its asset requirements'
`fulfilled_by`; they are separate records keyed the same way (requirement_id,
deliverable_id). Regenerating adds a new version; older versions are kept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .package import utcnow

ImageStatus = Literal["pending", "running", "done", "failed"]


class GeneratedImage(BaseModel):
    image_id: str
    package_id: str
    revision_id: str
    requirement_id: str
    deliverable_id: str
    version: int = 1
    status: ImageStatus = "pending"
    prompt: str
    # pending: `prompt` is the built-in template, to be rewritten by the text model before
    # generation; model: written by the text model; template: the built-in template is
    # used (offline, or the writer failed).
    prompt_source: Literal["pending", "model", "template"] = "pending"
    prompt_model: str | None = None  # the text model that wrote it
    # What the image must show, for the prompt writer: task, framing, reference roles and
    # the design-sheet facts.
    brief: dict[str, Any] = Field(default_factory=dict)
    job_id: str | None = None  # the job that made the package; its trace records the writer
    size: str  # WIDTHxHEIGHT
    seed: int
    # Images given to the model as <Picture N>, in order: other generated images
    # (image_id) or uploaded source images (asset_id).
    reference_ids: list[str] = Field(default_factory=list)
    model: str = ""
    file: str | None = None  # relative to the media store
    thumb: str | None = None
    content_type: str = "image/jpeg"
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
