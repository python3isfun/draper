"""Reference images generated for asset requirements (on demand, never part of a job).

A package revision is immutable, so images are not written into its asset requirements'
`fulfilled_by`; they are separate records keyed the same way (requirement_id,
deliverable_id).

Design exploration: each deliverable can have several *takes* (DesignOption: a distinct
interpretation with its own label, profile and prompt), and each take several *draws*
(GeneratedImage: one seed). The user selects one image per deliverable; a selected draft
is re-rendered at full size (`finalizes`). Dependent images (looks, other states) use the
selected anchor image and its take's profile.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .package import utcnow

ImageStatus = Literal["pending", "running", "done", "failed"]
OptionStatus = Literal["pending", "written", "template", "edited"]


class DesignOption(BaseModel):
    """One take on a deliverable: a distinct design with its own prompt. Its images are
    draws (different seeds) of that prompt."""

    option_id: str
    package_id: str
    revision_id: str
    requirement_id: str
    deliverable_id: str
    # Takes requested together (one exploration, or one "new take") share a group; the
    # prompt writer writes a group's takes in one go so they differ from each other.
    group_id: str
    index: int = 1  # display order within the deliverable
    # pending: not written yet (label/prompt are placeholders, the template); written: by
    # the text model; template: the built-in template (offline, or the writer failed);
    # edited: the user's own prompt, written over another take's (`edited_from`).
    status: OptionStatus = "pending"
    label: str = ""  # e.g. "Weathered steelworker"
    profile: str = ""  # what makes this take distinct, in a sentence or two
    prompt: str = ""
    prompt_model: str | None = None
    direction: str | None = None  # the user's steer for a new take ("older, kinder eyes")
    edited_from: str | None = None  # the take whose prompt the user edited into this one
    warnings: list[str] = Field(default_factory=list)  # rule checks an edited prompt fails
    brief: dict[str, Any] = Field(default_factory=dict)
    size: str = ""  # draft or full size for its draws
    job_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


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
    # used (offline, or the writer failed); user: the user's own edited prompt.
    prompt_source: Literal["pending", "model", "template", "user"] = "pending"
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
    # Design exploration (None/defaults for images made before it).
    option_id: str | None = None  # the take this image draws
    draw: int = 1
    selected: bool = False  # the chosen image for its deliverable (one per deliverable)
    finalizes: str | None = None  # the draft image this full-size render replaces
    model: str = ""
    file: str | None = None  # relative to the media store
    thumb: str | None = None
    content_type: str = "image/jpeg"
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
