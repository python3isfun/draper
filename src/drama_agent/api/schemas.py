"""Request and response models of the API, and helpers the route modules share."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..experiment import (
    MAX_DRAWS,
    ExpSession,
    ExpTarget,
    ExpTurn,
)
from ..experiment import MAX_PROMPT_CHARS as EXP_MAX_PROMPT_CHARS
from ..image_prompts import MAX_PROMPT_CHARS
from ..models import (
    ContentRating,
    Job,
    LLMCall,
    TraceEvent,
)
from ..models.images import DesignOption, GeneratedImage

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class RevalidatingStaticFiles(StaticFiles):
    """Static files the browser must revalidate on each load (ETag makes that cheap).

    Without Cache-Control, browsers cache app.js heuristically and keep running an old
    version of the UI after an update.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class JobCreated(BaseModel):
    job_id: str
    status: str


class JobSummary(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    seed: str
    package_id: str | None
    revision_id: str | None


class LLMCallView(BaseModel):
    """A model call for the live view: no request body; running calls carry an output tail."""

    call: LLMCall
    output_tail: str = ""


class Trace(BaseModel):
    events: list[TraceEvent]
    calls: list[LLMCallView]
    next_seq: int  # pass back as after_seq to get only newer events


OUTPUT_TAIL_CHARS = 4000


class Meta(BaseModel):
    model_id: str  # default model
    client: str
    stage_models: dict[str, str] = {}  # per-step overrides of the default
    image_model: str = ""
    image_client: str = ""


class ImagesView(BaseModel):
    package_id: str
    revision_id: str
    image_model: str
    images: list[GeneratedImage]
    # Over the current (newest) version of each deliverable.
    pending: int
    running: int
    done: int
    failed: int
    # Design exploration: the takes, the deliverables (anchor or drawn from one), and the
    # whole queue (every draw counts).
    options: list[DesignOption] = []
    slots: list[dict] = []
    queued: int = 0  # pending + running images of any version or draw
    notes: list[str] = []
    # Picks that are out of date (image id -> why): drawn from earlier design text, or from
    # a picture that was replaced or is out of date itself.
    outdated: dict[str, list[str]] = {}


class ExploreRequest(BaseModel):
    takes: int = Field(4, ge=1, le=6)
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    scope: list[Literal["cast", "sets", "props"]] = ["cast", "sets", "props"]
    revision_id: str | None = None
    job_id: str | None = None


class ImportImageRequest(BaseModel):
    """An Experiment image (#N in a chat) as the pick for a package deliverable."""

    session_id: str
    image: int = Field(..., ge=1)  # the image's number in the chat (#N)
    requirement_id: str
    deliverable_id: str | None = None  # default: the requirement's first deliverable
    # None: as the Experiment recorded it (unknown counts as a likeness). False is for an
    # invented person whose image inherited the mark from someone else in its source.
    likeness: bool | None = None
    revision_id: str | None = None
    job_id: str | None = None


class EntryEditRequest(BaseModel):
    """A hand edit to one cast, set or prop entry. `sheet` holds design sheet fields
    (lists as lists of strings); `bible` a look's or state's one-line `description`."""

    revision_id: str
    sheet: dict[str, Any] = Field(default_factory=dict)
    bible: dict[str, Any] = Field(default_factory=dict)


class EntryEditResult(BaseModel):
    package_id: str
    revision_id: str
    entry_id: str
    changed: list[str]
    requirement_ids: list[str]  # requirements whose design text was rebuilt
    # Of those, the ones whose model-written design text the rebuild replaced.
    replaced_written: list[str]
    drawn_before: list[str]  # board items with images drawn from the earlier text


class DevelopRequest(BaseModel):
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    revision_id: str | None = None
    job_id: str | None = None


class NewTakeRequest(BaseModel):
    requirement_id: str
    deliverable_id: str
    direction: str | None = Field(None, max_length=500)
    draws: int = Field(3, ge=1, le=6)
    draft: bool = True
    revision_id: str | None = None
    job_id: str | None = None


class DrawsRequest(BaseModel):
    draws: int = Field(3, ge=1, le=6)


class EditTakeRequest(BaseModel):
    prompt: str = Field(..., max_length=MAX_PROMPT_CHARS)
    draws: int | None = Field(None, ge=1, le=6)  # default: as many as the source take has


class ExpSessionCreate(BaseModel):
    title: str | None = Field(None, max_length=120)
    kind: Literal["chat", "face"] = "chat"  # face: a Face Lab study
    # The design picture it is for (docs/workbench.md); a chat starts from its pick.
    target: ExpTarget | None = None
    start_from_pick: bool = True
    # Open the picture's untouched chat (no message yet, started from its current pick) or
    # face study (nothing in it yet) instead of starting another one: "Develop in a chat"
    # or "Create from text" clicked again.
    reuse_empty: bool = False
    # A keyframe chat: the draw it starts from ("<take_id>:<n>"); else the sub-shot's
    # approved keyframe, else its newest finished draw.
    from_frame: str | None = None


class UsePickRequest(BaseModel):
    """A chat's image (#N) as the pick for the design picture the chat is for."""

    image: int = Field(..., ge=1)
    # As in ImportImageRequest: None keeps the likeness the chat recorded.
    likeness: bool | None = None


class FaceDescribeRequest(BaseModel):
    asset_id: str | None = None  # a new photo; default: the study's photo again
    image: int | None = Field(None, ge=1)  # or any picture by its global number (@1234)
    hint: str = Field("", max_length=500)  # what to focus on ("the eyes", "older")
    restrictive: bool = False


class FaceTestRequest(BaseModel):
    prompt: str = Field(..., max_length=EXP_MAX_PROMPT_CHARS)
    draws: int = Field(1, ge=1, le=MAX_DRAWS)
    seeds: list[int] | None = Field(None, max_length=MAX_DRAWS)  # fixed seeds (one per draw)
    size: str | None = None
    restrictive: bool = False
    # The image provider: "h3" (default) or another the study lists (e.g. "local").
    provider: str | None = Field(None, max_length=40)


class RatingBody(BaseModel):
    rating: ContentRating  # an MPA film rating: G, PG, PG-13 or R


class ExpSessionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=120)
    size: str | None = None  # "auto" or one of EXP_SIZES
    current: str | None = None  # an image of the session: "asset:<id>" or "turn:<id>:<n>"
    clear_current: bool = False  # the next message makes a new image (text to image)
    # Link a scratch chat or study to a design picture (docs/workbench.md), or unlink it.
    target: ExpTarget | None = None
    unlink: bool = False


class ExpTurnRequest(BaseModel):
    text: str = Field("", max_length=EXP_MAX_PROMPT_CHARS)
    attachments: list[str] = Field(default_factory=list, max_length=9)  # source asset ids
    mode: Literal["rewrite", "exact"] = "rewrite"
    draws: int = Field(1, ge=1, le=MAX_DRAWS)
    size: str | None = None  # overrides the session's size for this message
    # Restrictive mode (top-of-page switch, off by default): no real-person likeness by name
    # or face. The content rules that protect minors and real people apply either way.
    restrictive: bool = False


class ExpSessionItem(ExpSession):
    """A chat or study in a list: with how many messages it has (the pick start is not
    one), so the history can tell an untouched chat from one in use."""

    messages: int = 0


class ExpSessionView(BaseModel):
    session: ExpSession
    turns: list[ExpTurn]
    sizes: dict[str, str]  # aspect ratio -> size
    # Image providers a Face Lab test can be drawn with (name -> model), H3 first.
    providers: dict[str, str] = Field(default_factory=dict)


EXPORT_DIR = Path(tempfile.gettempdir()) / "drama-exports"
EXPORT_MAX_AGE_S = 3600


def _new_export_file() -> Path:
    """A fresh temp file for an export zip. Zips are deleted after they are sent; one left
    behind by a cancelled download (the cleanup task doesn't run then) is swept here once
    it is an hour old."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - EXPORT_MAX_AGE_S
    for old in EXPORT_DIR.glob("drama-export-*.zip"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    fd, path = tempfile.mkstemp(prefix="drama-export-", suffix=".zip", dir=EXPORT_DIR)
    os.close(fd)  # build_export reopens it by path
    return Path(path)


def _seed_label(job: Job, assets: dict[str, dict]) -> str:
    """Job list label: the first text, else the first uploaded file's name."""
    sources = job.request.sources
    text = next((s.text for s in sources if s.type in ("text", "script") and s.text), None)
    if text:
        return text
    prompt = next((s.text for s in sources if s.text), None)
    first = sources[0]
    name = (assets.get(first.asset_id or "") or {}).get("filename") or first.asset_id or ""
    label = f"[{first.type}] {name}"
    return f"{label} · {prompt}" if prompt else label


class AddVariantRequest(BaseModel):
    """A new look for a character, or a new state for a location, added by hand.
    `sheet` holds design sheet fields (a look's wardrobe, hair_and_makeup, condition; a
    state's time_of_day, lighting, dressing_changes, condition)."""

    parent_id: str  # the story's character_id or location_id
    description: str = Field(..., min_length=1, max_length=500)
    sheet: dict[str, Any] = Field(default_factory=dict)
    revision_id: str | None = None


class AddVariantResult(BaseModel):
    package_id: str
    revision_id: str
    entry_id: str  # the new look_id or location_state_id
    requirement_id: str
    deliverable_ids: list[str]
    # Its Characters or Sets page: the character or set (`lib`) and the look or state (`part`).
    lib_asset_id: str | None = None
    part_asset_id: str | None = None


class DesignTarget(BaseModel):
    """A design picture a keyframe draw can be sent to as a take: a look the shot's
    characters wear, or the set state it is in."""

    requirement_id: str
    deliverable_id: str
    kind: Literal["look", "set"]
    label: str  # "Lila Montgomery, evening torn look, full body"
    sent_image_id: str | None = None  # already sent: the design image it became
    picked: bool = False  # ... and that image is the pick
    lib_asset_id: str | None = None  # its Characters or Sets page
    part_asset_id: str | None = None


class ToDesignRequest(BaseModel):
    requirement_id: str
    deliverable_id: str
    pick: bool = False  # also make it the picture's pick


class UseKeyframeRequest(BaseModel):
    """A keyframe chat's image (#N) as a take of its sub-shot, approved unless `approve` is
    false."""

    image: int = Field(..., ge=1)
    approve: bool = True


class AddItemRequest(BaseModel):
    """A new item a character carries, added by hand. `sheet` holds prop sheet fields
    (size_and_scale, materials, color_and_finish, condition_and_wear, distinguishing_marks,
    handling); `state` is its first state's line (default "As it first appears")."""

    owner_id: str  # the story's character_id
    name: str = Field(..., min_length=1, max_length=120)
    story_function: str = Field("", max_length=500)
    sheet: dict[str, Any] = Field(default_factory=dict)
    state: str = Field("", max_length=300)
    revision_id: str | None = None


class AddItemResult(BaseModel):
    package_id: str
    revision_id: str
    prop_id: str
    state_id: str
    requirement_id: str
    deliverable_ids: list[str]
    lib_asset_id: str | None = None  # its character's page ...
    part_asset_id: str | None = None  # ... and the item on it
