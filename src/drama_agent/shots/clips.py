"""A shot's clip: the video rendered from its video prompt and its reference pictures.

The stored video prompt (video_prompt.py) is one block per cut, each picture named by its
ref id (`{pic:<ref>}`), with the sound sentence apart. `clip_prompt` turns it into the one
paragraph H3 takes: `[Shot 1] ...`, `[Shot 2] At 00:02.000, ...` from the plan's timecodes,
each picture as `<Picture N>` in the order the pictures are sent (clip_refs.py), and the
sentences of a picture that isn't sent dropped, as dramer does. A clip is current while
the prompt it was rendered from is the shot's current prompt and the same pictures would
be sent; a change to either makes it out of date (`clip_state`), like a video prompt.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import ProductionPackage
from ..models.package import utcnow
from ..videogen import H3_RATIOS, clamp_seconds
from .models import ShotPlan, ShotVideoPrompt
from .video_prompt import PIC

ClipStatus = Literal["pending", "rendering", "done", "failed"]

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class ClipRef(BaseModel):
    """A picture sent with the clip: its ref id and the label the prompt calls it by."""

    ref_id: str
    label: str  # "<Picture 3>"
    kind: str = ""
    about: str = ""


class ShotClip(BaseModel):
    """One render of a shot's clip. `prompt` is the paragraph sent, `references` the
    pictures in the order sent; `written_from` fingerprints both so a later change to the
    video prompt or the pictures marks the clip out of date."""

    clip_id: str = Field(default_factory=lambda: f"clp_{uuid.uuid4().hex[:12]}")
    package_id: str
    revision_id: str
    shot_id: str
    status: ClipStatus = "pending"
    prompt: str = ""
    references: list[ClipRef] = Field(default_factory=list)
    duration_s: int = 4
    ratio: str = "9:16"
    file: str | None = None
    poster: str | None = None
    content_type: str = "video/mp4"
    model: str | None = None
    task_id: str | None = None
    written_from: str = ""
    run_id: str | None = None  # the episode run that rendered it, if one did
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def busy(self) -> bool:
        return self.status in ("pending", "rendering")


def clip_ratio(pkg: ProductionPackage) -> str:
    """The video's aspect ratio as H3 names it (9:16 unless the brief says else)."""
    m = re.search(r"(\d+)\s*:\s*(\d+)", pkg.asset_requirements.style_brief.aspect_ratio or "")
    ratio = f"{m.group(1)}:{m.group(2)}" if m else "9:16"
    return ratio if ratio in H3_RATIOS else "9:16"


def clip_seconds(plan: ShotPlan, shot_ms: int) -> int:
    """The clip's length in whole seconds: the plan's, else the storyboard's."""
    return clamp_seconds(plan.sub_shots[-1].end_ms if plan.sub_shots else shot_ms)


def _camera_sentence(camera: str) -> str:
    camera = camera.strip().rstrip(".")
    if not camera:
        return ""
    if camera.lower().startswith("the camera"):
        return camera + "."
    if "static" in camera.lower():
        return "The camera holds a static shot."
    return f"Camera motion: {camera}."


def clip_prompt(vp: ShotVideoPrompt, plan: ShotPlan, sent: list[str]) -> str:
    """The paragraph H3 takes, from the stored prompt's blocks: `[Shot N]` with the plan's
    timecode from the second cut on, pictures as `<Picture N>` in the order of `sent` (ref
    ids), the sentences naming a picture not sent left out, each block's camera sentence
    after its text, and the sound sentence last."""
    labels = {ref: f"<Picture {i}>" for i, ref in enumerate(sent, 1)}
    parts = []
    for block in sorted(vp.blocks, key=lambda b: b.sub_shot):
        sub = plan.sub(block.sub_shot)
        kept = []
        for sentence in _SENTENCE.split(block.text.strip()):
            pics = PIC.findall(sentence)
            if any(p not in labels for p in pics):
                continue
            kept.append(PIC.sub(lambda m: labels[m.group(1)], sentence))
        text = " ".join(s for s in kept if s)
        if (cam := _camera_sentence(block.camera)) and cam.lower() not in text.lower():
            text = f"{text} {cam}".strip()
        head = f"[Shot {block.sub_shot}]"
        if block.sub_shot > 1 and sub is not None:
            head += f" At {sub.timecode}, "
        else:
            head += " "
        parts.append(head + text)
    if sound := vp.sound.strip():
        parts.append(sound)
    return " ".join(parts).strip()


def clip_print(vp: ShotVideoPrompt, prompt: str, sent: list[str], duration_s: int) -> str:
    """What a clip is rendered from: the prompt as written (its inputs' fingerprint and its
    text) and the pictures sent, in order."""
    h = hashlib.sha256()
    for part in (vp.written_from, prompt, str(duration_s), *sent):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()[:24]


def clip_state(clip: ShotClip | None, now: str | None) -> str:
    """none, rendering, failed, current (rendered from the shot as it is now) or outdated.
    `now` is the clip print the shot would be rendered with (None: it can't be rendered
    yet, so any finished clip is out of date)."""
    if clip is None:
        return "none"
    if clip.busy:
        return "rendering"
    if clip.status == "failed":
        return "failed"
    return "current" if now is not None and clip.written_from == now else "outdated"


def latest_clip(clips: list[ShotClip], shot_id: str) -> ShotClip | None:
    mine = [c for c in clips if c.shot_id == shot_id]
    return max(mine, key=lambda c: c.created_at, default=None)


def clip_view(clip: ShotClip | None, state: str) -> dict[str, Any]:
    """A clip for the board and the episode page."""
    out: dict[str, Any] = {"state": state}
    if clip is not None:
        out |= clip.model_dump(mode="json", include={
            "clip_id", "status", "duration_s", "ratio", "model", "error", "created_at",
            "finished_at", "run_id"})  # fmt: skip
        out["has_file"] = bool(clip.file)
        out["has_poster"] = bool(clip.poster)
        out["pictures"] = [r.model_dump() for r in clip.references]
    return out
