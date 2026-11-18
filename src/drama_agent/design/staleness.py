"""What is out of date after a design change, and redrawing it in order."""

from __future__ import annotations

from typing import Any

from ..models import ProductionPackage
from ..models.images import GeneratedImage
from .prompts import _prompt_and_refs, _Slot, _slots, key_of, latest_images, plan_images
from .slots import selected_image
from .takes import _is_draft

# What the out-of-date rules say (design pictures and keyframes).
TEXT_CHANGED = "drawn from earlier design text"

PICTURE_REPLACED = "drawn from a picture that has since been replaced"

PICTURE_OUTDATED = "drawn from a picture that is out of date"  # waits for that one first

# The parts of an image's brief that come from the design text (sheets and requirement).
DESIGN_KEYS = ("subject_facts", "variant_facts", "must_keep", "requirement_brief")


def design_text(
    pkg: ProductionPackage, key: tuple[str, str], slots: list[_Slot] | None = None
) -> dict[str, Any] | None:
    """The design text a picture of this deliverable is drawn from now (what its brief
    records), or None for a deliverable the package doesn't plan."""
    slots = slots if slots is not None else _slots(pkg)
    slot = next((s for s in slots if s.key == key), None)
    if slot is None:
        return None
    _, _, brief = _prompt_and_refs(pkg, slot, slots, lambda _k: None)
    return {k: brief[k] for k in DESIGN_KEYS}


def is_current(images: list[GeneratedImage], image_id: str) -> bool:
    """Whether an image is still its deliverable's pick (or the draft whose full-size
    render is the pick now)."""
    img = next((i for i in images if i.image_id == image_id), None)
    if img is None:
        return False
    pick = selected_image(images, key_of(img))
    return pick is not None and image_id in (pick.image_id, pick.finalizes)


def _sources(
    img: GeneratedImage, images: list[GeneratedImage], by_id: dict[str, GeneratedImage]
) -> list[tuple[GeneratedImage | None, bool]]:
    """The pictures an image was drawn from, each with whether it was the pick when the
    image was drawn: a fixed reference (an image id), a live one ("sel:<requirement>|
    <deliverable>", whatever was picked at draw time: judged against the pick now, which is
    newer than the image if it was replaced since), and for a full-size render, the
    pictures its draft was drawn from (the render keeps the draft as it is)."""
    out: list[tuple[GeneratedImage | None, bool]] = []
    drawn_at = img.started_at or img.created_at
    for rid in img.reference_ids:
        if rid == img.finalizes:
            if (draft := by_id.get(rid)) is not None:
                out += _sources(draft, images, by_id)
        elif rid.startswith("img_"):
            out.append((by_id.get(rid), is_current(images, rid)))
        elif rid.startswith("sel:"):
            req_id, _, deliverable_id = rid[4:].partition("|")
            pick = selected_image(images, (req_id, deliverable_id))
            if pick is None:
                continue
            # A full-size render stands for its draft, which is what was picked then.
            first = by_id.get(pick.finalizes or "") or pick
            made = first.finished_at or first.created_at
            out.append((pick, made <= drawn_at))
    return out


def outdated_pictures(pkg: ProductionPackage, images: list[GeneratedImage]) -> dict[str, list[str]]:
    """Each deliverable's pick that is out of date, by image id, with why: its design text
    changed since it was drawn (a hand edit), or a picture it was drawn from was replaced
    or is out of date itself. Images that record no design text (imported ones, or drawn
    before briefs recorded it) are judged by their pictures alone."""
    slots = _slots(pkg)
    texts: dict[tuple[str, str], dict[str, Any] | None] = {}
    picks = {k: i for k, i in latest_images(images).items() if i.status == "done"}
    by_id = {i.image_id: i for i in images}
    memo: dict[str, list[str]] = {}

    def reasons(img: GeneratedImage, seen: frozenset[str]) -> list[str]:
        if img.image_id in memo:
            return memo[img.image_id]
        out: list[str] = []
        if all(k in img.brief for k in DESIGN_KEYS):
            key = key_of(img)
            if key not in texts:
                texts[key] = design_text(pkg, key, slots)
            now = texts[key]
            if now is not None and any(img.brief.get(k) != now[k] for k in DESIGN_KEYS):
                out.append(TEXT_CHANGED)
        for ref, current in _sources(img, images, by_id):
            if ref is None or ref.image_id in seen:
                continue
            if not current:
                out.append(PICTURE_REPLACED)
            elif reasons(ref, seen | {img.image_id}):
                out.append(PICTURE_OUTDATED)
        memo[img.image_id] = list(dict.fromkeys(out))
        return memo[img.image_id]

    return {i.image_id: r for i in picks.values() if (r := reasons(i, frozenset()))}


def drawn_from_any(
    img: GeneratedImage, images: list[GeneratedImage], keys: set[tuple[str, str]]
) -> bool:
    """Whether an image was drawn from a picture of one of these deliverables."""
    by_id = {i.image_id: i for i in images}
    return any(ref is not None and key_of(ref) in keys for ref, _ in _sources(img, images, by_id))


def plan_refresh(
    pkg: ProductionPackage,
    images: list[GeneratedImage],
    *,
    drawn_from: set[tuple[str, str]] | None = None,
    only: set[str] | None = None,
) -> list[GeneratedImage]:
    """Redraws of the out-of-date picks that can be redrawn now: not those drawn from a
    picture that is out of date itself (they follow once it is redrawn, see ImageWorker),
    nor those already being redrawn. With `drawn_from`, only picks drawn from a picture of
    one of those deliverables. Each redraw takes over its pick when done. A prompt written by hand is kept; a
    model-written one is written again when the design text changed. Same size class as
    the pick (draft or full)."""
    stale = outdated_pictures(pkg, images)
    busy = {key_of(i) for i in images if i.status in ("pending", "running")}
    out: list[GeneratedImage] = []
    for key, pick in latest_images(images).items():
        why = stale.get(pick.image_id) if pick.status == "done" else None
        if not why or PICTURE_OUTDATED in why or key in busy:
            continue
        if drawn_from is not None and not drawn_from_any(pick, images, drawn_from):
            continue
        if only is not None and key[0] not in only:
            continue  # another asset's picture (the pictures drawn from these follow anyway)
        planned = plan_images(pkg, images, draft=_is_draft(pick.size), only=key,
                              job_id=pick.job_id, rewrite=TEXT_CHANGED in why)  # fmt: skip
        if not planned:
            continue
        new = planned[0]
        if pick.prompt_source == "user":
            new.prompt, new.prompt_source = pick.prompt, "user"
        new.refreshes = pick.image_id
        out.append(new)
    return out
