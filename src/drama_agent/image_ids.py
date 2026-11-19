"""Global image numbers: every picture in the app has one ("@1234"), so a chat message can
bring in any of them, from another chat, a story's design pictures, a keyframe or an upload
(docs/image-ids.md).

A number belongs to a ref, the picture's own address, and is handed out the first time the
picture is shown (store `image_ids`); it never changes. Refs:

- `asset:<asset_id>`: an uploaded image
- `turn:<turn_id>:<n>`: draw n of a chat or face-study message
- `design:<image_id>`: a story's design image (a candidate or the pick)
- `take:<take_id>:<n>`: draw n of a keyframe take
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .models.media import SourceAsset

RefKind = Literal["asset", "turn", "design", "take"]

# "@1234" outside a word or e-mail address; up to 7 digits.
MENTION = re.compile(r"(?<![\w@#])@(\d{1,7})(?![\w])")
MAX_REFS = 500  # per lookup


def parse(ref: str) -> tuple[RefKind, str, int] | None:
    """(kind, id, draw) of a well-formed ref, else None."""
    kind, _, rest = ref.partition(":")
    if kind in ("asset", "design") and rest and ":" not in rest:
        return kind, rest, 0  # type: ignore[return-value]
    if kind in ("turn", "take"):
        ident, _, n = rest.rpartition(":")
        if ident and n.isdigit():
            return kind, ident, int(n)  # type: ignore[return-value]
    return None


@dataclass
class Found:
    """A picture a global number points at."""

    ref: str
    file: str  # relative to the media store
    thumb: str | None
    content_type: str
    what: str  # e.g. "a design picture of Lila Montgomery, front face"
    prompt: str | None  # the prompt that made it, if it was drawn
    # Shows a real person's likeness: True/False when known, None when the picture must be
    # judged (an upload, or a result made before likeness was tracked).
    likeness: bool | None
    from_upload: bool  # an upload, or made at any remove from one
    size: str = ""  # "WIDTHxHEIGHT" when known: an automatic size follows its shape


def resolve(repo: Any, ref: str) -> Found | None:
    """The picture behind a ref, if it (still) exists and has a file."""
    parsed = parse(ref)
    if parsed is None:
        return None
    kind, ident, n = parsed
    if kind == "asset":
        raw = repo.get_source_asset(ident)
        if not raw or raw.get("kind") != "image":
            return None
        a = SourceAsset.model_validate(raw)
        # Copied in as a chat result, which goes to the image model as it is: a WebP or
        # HEIC upload goes as its JPEG copy.
        as_is = a.content_type in ("image/jpeg", "image/png") or not a.model_image
        file, mime = (a.file, a.content_type) if as_is else (a.model_image, "image/jpeg")
        size = f"{a.width}x{a.height}" if a.width and a.height else ""
        return Found(ref, file, a.model_image, mime, f"an upload ({a.filename})",  # type: ignore[arg-type]
                     None, None, True, size)  # fmt: skip
    if kind == "turn":
        from .experiment import _from_upload

        t = repo.get_exp_turn(ident)
        r = t.results[n] if t and n < len(t.results) else None
        if t is None or r is None or not r.file:
            return None
        session = repo.get_exp_session(t.session_id)
        title = session.title if session else "a deleted chat"
        label = f" #{r.label}" if r.label is not None else ""
        turns = repo.list_exp_turns(t.session_id)
        return Found(ref, r.file, r.thumb, r.content_type, f'image{label} of the chat "{title}"',
                     t.prompt or None, t.likeness, _from_upload(ref, turns), t.size)  # fmt: skip
    if kind == "design":
        img = repo.get_image(ident)
        if img is None or img.status != "done" or not img.file:
            return None
        return Found(ref, img.file, img.thumb, img.content_type, _design_what(repo, img),
                     img.prompt, _story_likeness(repo, img.package_id, img.likeness),
                     bool(img.imported_from) or _has_upload(img), img.size)  # fmt: skip
    take = repo.get_shot_take(ident)
    d = next((d for d in take.results if d.n == n), None) if take else None
    if take is None or d is None or d.status != "done" or not d.file:
        return None
    return Found(ref, d.file, d.thumb, d.content_type,
                 f"keyframe draw {n + 1} of shot {take.shot_id}", take.prompt or None,
                 _story_likeness(repo, take.package_id, take.likeness), False,
                 take.size)  # fmt: skip


def _story_likeness(repo: Any, package_id: str, marked: bool) -> bool | None:
    """A story picture's likeness as a chat takes it: marked only when drawn from one, so in
    a story with uploaded photos an unmarked picture is "unknown" (judged from the picture),
    as a chat started from a pick takes it (api/experiments.py)."""
    from .design.setup import package_has_uploads

    if marked:
        return True
    return None if package_has_uploads(repo, package_id) else False


def _has_upload(img: Any) -> bool:
    """Whether a design image was drawn with an uploaded photo among its references
    (asset ids start "asset_"; image ids "img_")."""
    return any(r.startswith("asset_") for r in img.reference_ids)


def _design_what(repo: Any, img: Any) -> str:
    """ "a design picture of Lila Montgomery, front face (story title)"."""
    from .design.slots import slot_title

    pkg = repo.get_package(img.package_id, img.revision_id) or repo.get_package(img.package_id)
    if pkg is None:
        return f"a design picture ({img.deliverable_id})"
    title = slot_title(pkg, (img.requirement_id, img.deliverable_id))
    return f'a design picture of {title} (story "{pkg.story_bible.title}")'
