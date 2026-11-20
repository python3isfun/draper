"""Experiment: chat sessions that change an image with MiniMax-H3-Image, one message at a time.

A session keeps a *current image*. Each message (turn) sends the current image as
<Picture 1>, followed by any images attached to the message (<Picture 2>, ...), with a
prompt:

- "rewrite": the text model turns the message into a finished H3 prompt. It sees the
  pictures, the prompt that made the current image and the session's earlier messages, so
  "make her smile a little more" becomes a full edit description (our GPU route renders
  prompts as written; nothing expands them).
- "exact": the message is the prompt, sent as written (to try prompts by hand).
- "upload": a message with images and no text only sets the current image.
- "pick": the first turn of a chat started from a design slot's pick (docs/workbench.md):
  nothing is drawn; its one result is a copy of the pick, with the pick's prompt and
  likeness, so the chat builds on it like on any result.

With no current image and no attachments, a message makes a new image (text to image).
When a turn finishes, its first result becomes the current image; any result, or any
uploaded image, can be made current instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .content_guard import GuardRefused, mode_rule, screen, text_guard
from .content_guard import rules as content_rules
from .image_ids import MENTION as GLOBAL_MENTION
from .imagegen import (
    MAX_REFERENCE_BYTES,
    MAX_REFERENCES,
    ImageClient,
    ImageGenError,
    Reference,
)
from .llm import GenerationRequest
from .media import MediaPart, MediaStore
from .models import SourceAsset
from .models.package import utcnow
from .models.request import DEFAULT_RATING

log = logging.getLogger(__name__)

STAGE = "experiment"
MAX_PROMPT_CHARS = 6000  # H3 reads at most ~4,096 tokens
MAX_DRAWS = 4
HISTORY_TURNS = 8  # earlier messages shown to the prompt writer

# H3 sizes at about 4 MP ("2K", both sides multiples of 32), by aspect ratio: a chat is for
# quick iteration, and a design pick is drawn at about this size too (design/prompts.py).
SIZES: dict[str, str] = {
    "1:1": "2048x2048",
    "3:4": "1728x2304",
    "4:3": "2304x1728",
    "2:3": "1664x2496",
    "3:2": "2496x1664",
    "9:16": "1536x2720",
    "16:9": "2720x1536",
    "21:9": "3136x1344",
}
DEFAULT_SIZE = SIZES["1:1"]
# The earlier 16 MP sizes, which chats and studies made before may have saved.
_OLD_SIZES: dict[str, str] = {
    "4096x4096": "1:1", "3552x4736": "3:4", "4736x3552": "4:3", "3360x5024": "2:3",
    "5024x3360": "3:2", "3072x5472": "9:16", "5472x3072": "16:9", "6272x2688": "21:9",
}  # fmt: skip


def current_size(size: str) -> str:
    """A saved size as it is now: an earlier 16 MP size becomes the same aspect's 2K one."""
    return SIZES[_OLD_SIZES[size]] if size in _OLD_SIZES else size


TurnStatus = Literal["pending", "running", "done", "failed"]
# describe: Face Lab's photo-to-prompt step (draws nothing).
# import: an image brought in from elsewhere by its global number ("@1234", image_ids.py).
TurnMode = Literal["rewrite", "exact", "upload", "describe", "pick", "import"]
PictureVariant = Literal["full", "model", "thumb"]


class ExpResult(BaseModel):
    label: int | None = None  # the image's number in the chat (#7); None before labels existed
    file: str
    thumb: str | None = None
    content_type: str = "image/jpeg"
    seed: int


class ExpTurn(BaseModel):
    turn_id: str
    session_id: str
    index: int  # 1, 2, ... within the session
    text: str = ""  # the user's message
    attachments: list[str] = Field(default_factory=list)  # source asset ids
    attachment_labels: list[int] = Field(default_factory=list)  # their numbers (#N), in order
    # The pictures sent, in <Picture N> order: "asset:<id>" or "turn:<turn_id>:<n>".
    references: list[str] = Field(default_factory=list)
    # The pictures the prompt writer was shown, sent or not: lineage counts them too, so a
    # result can't lose "from an upload" by the writer describing a photo it didn't send.
    offered: list[str] = Field(default_factory=list)
    mode: TurnMode = "rewrite"
    size: str = ""
    auto_size: bool = False  # size follows <Picture 1> (it's re-picked if the writer reorders)
    draws: int = 1
    seeds: list[int] = Field(default_factory=list)  # fixed seeds, one per draw (else random)
    prompt: str = ""  # what H3 received
    prompt_model: str | None = None  # the text model that wrote it ("rewrite")
    reply: str = ""  # the writer's short note on what it changed
    status: TurnStatus = "pending"
    error: str | None = None
    refused: bool = False  # the content guard refused it (retrying won't help)
    # Any picture is an upload or was made from one (provenance, for information).
    from_upload: bool = False
    # Whether the result shows a real person's likeness (an identifiable person from an
    # uploaded photo, or someone made to resemble a named real person): content rule 3 then
    # applies to images made from it. None: made before this was tracked.
    likeness: bool | None = None
    # Restrictive mode (top-of-page switch) when the message was sent.
    restrictive: bool = False
    # The story's rating (G, PG, PG-13, R) when it was sent, which caps what it may show;
    # PG-13 outside a story (models/request.py, content_guard.py).
    rating: str = DEFAULT_RATING
    from_image: str | None = None  # "pick": the design image it copies (its image_id)
    import_ref: str | None = None  # "import": the picture it copies (an image_ids ref)
    # The image provider that draws it: None for H3, or another by name ("nano-banana",
    # Gemini's image models; "local", a simple image server of your own: tests only).
    provider: str | None = None
    # Global numbers the message mentions ("@1234") and the chat number each came in as.
    global_mentions: dict[str, int] = Field(default_factory=dict)
    results: list[ExpResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class ExpTarget(BaseModel):
    """What a chat or study works on (docs/workbench.md): a story's design picture, by the
    requirement and deliverable ids, which stay the same across revisions; or a keyframe,
    one sub-shot of a shot (docs/library-layers.md), with `shot_id` and `sub_shot`."""

    package_id: str
    requirement_id: str = ""
    deliverable_id: str = ""
    shot_id: str | None = None  # a keyframe: the shot ...
    sub_shot: int | None = None  # ... and its sub-shot (1-based)
    label: str = ""  # e.g. "Derek Blackwood, face"; "sh_02_01_05, keyframe 1"

    @property
    def keyframe(self) -> bool:
        return self.shot_id is not None

    @property
    def key(self) -> tuple[str, str, str, str | None, int | None]:
        """The same picture (or keyframe) in any revision of the story."""
        return (self.package_id, self.requirement_id, self.deliverable_id, self.shot_id,
                self.sub_shot)  # fmt: skip


class ExpSession(BaseModel):
    session_id: str
    kind: Literal["chat", "face"] = "chat"  # face: a Face Lab study (see face_lab.py)
    # A study's subject (face_lab.SUBJECTS), from the picture it is for: how a photo is
    # described and what its tests are drawn from. "face" for a study of no picture.
    subject: Literal["face", "look", "set", "plan", "prop"] = "face"
    target: ExpTarget | None = None  # the design picture it is for; None: a free experiment
    title: str = "New chat"
    size: str = "auto"  # "auto": the current image's aspect ratio
    current: str | None = None  # the image the next message changes (a reference string)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExpError(ValueError):
    """A request the session can't take (shown to the user)."""


def new_session(title: str | None = None) -> ExpSession:
    return ExpSession(session_id=f"exp_{uuid.uuid4().hex[:12]}", title=title or "New chat")


def pick_turn(
    repo: Any,
    media: MediaStore,
    session: ExpSession,
    pick: Any,
    label: str,
    likeness: bool | None,
) -> ExpTurn:
    """Start a chat from a design slot's pick (a GeneratedImage): a first turn that draws
    nothing, whose one result (#1) is a copy of the pick with its prompt and the given
    likeness (None: unknown, judged from the picture). It becomes the session's current
    image. Copies files (blocking)."""
    turn = ExpTurn(
        turn_id=f"turn_{uuid.uuid4().hex[:12]}",
        session_id=session.session_id,
        index=1,
        text=(
            f"Started from {'a draw' if str(pick.image_id).startswith('frame:') else 'the current pick'}"
            f" of {label}."
            if label
            else "Started from the pick."
        ),
        mode="pick",
        from_image=pick.image_id,
        size=size_of(pick.size),
        prompt=pick.prompt,
        likeness=likeness,
        status="done",
        finished_at=utcnow(),
    )
    base = f"experiments/{session.session_id}/{turn.turn_id}_0"
    rel = base + (Path(pick.file).suffix or ".jpg")
    dest = media.path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(media.path(pick.file), dest)
    thumb = None
    if pick.thumb and media.path(pick.thumb).is_file():
        thumb = base + "_thumb.jpg"
        shutil.copyfile(media.path(pick.thumb), media.path(thumb))
    [n] = repo.allocate_exp_labels(session.session_id, 1)
    turn.results = [ExpResult(label=n, file=rel, thumb=thumb, content_type=pick.content_type,
                              seed=0)]  # fmt: skip
    repo.save_exp_turn(turn)
    session.current = f"turn:{turn.turn_id}:0"
    session.updated_at = utcnow()
    repo.save_exp_session(session)
    return turn


def import_turn(
    repo: Any, media: MediaStore, session: ExpSession, found: Any, gid: int, index: int
) -> ExpTurn:
    """An image brought into a chat by its global number (an image_ids.Found): a turn that
    draws nothing, whose one result is a copy with the prompt and likeness it was made
    with. Unlike a pick start it doesn't become the current image. Not saved; copies files
    (blocking)."""
    turn = ExpTurn(
        turn_id=f"turn_{uuid.uuid4().hex[:12]}",
        session_id=session.session_id,
        index=index,
        text=f"Brought in @{gid}: {found.what}.",
        mode="import",
        size=size_of(found.size),
        import_ref=found.ref,
        prompt=found.prompt or "",
        likeness=found.likeness,
        from_upload=found.from_upload,
        status="done",
        finished_at=utcnow(),
    )
    base = f"experiments/{session.session_id}/{turn.turn_id}_0"
    rel = base + (Path(found.file).suffix or ".jpg")
    dest = media.path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(media.path(found.file), dest)
    thumb = None
    if found.thumb and media.path(found.thumb).is_file():
        thumb = base + "_thumb.jpg"
        shutil.copyfile(media.path(found.thumb), media.path(thumb))
    [n] = repo.allocate_exp_labels(session.session_id, 1)
    turn.results = [ExpResult(label=n, file=rel, thumb=thumb, content_type=found.content_type,
                              seed=0)]  # fmt: skip
    return turn


def is_busy(turns: list[ExpTurn]) -> bool:
    return any(t.status in ("pending", "running") for t in turns)


def parse_ref(ref: str) -> tuple[str, str, int]:
    """("asset", asset_id, 0) or ("turn", turn_id, n)."""
    kind, _, rest = ref.partition(":")
    if kind == "asset" and rest:
        return "asset", rest, 0
    if kind == "turn":
        tid, _, n = rest.rpartition(":")
        if tid and n.isdigit():
            return "turn", tid, int(n)
    raise ExpError(f"not an image reference: {ref}")


def known_refs(turns: list[ExpTurn]) -> set[str]:
    """Images of a session that can be made current: uploads and finished results."""
    refs: set[str] = set()
    for t in turns:
        refs.update(f"asset:{a}" for a in t.attachments)
        refs.update(f"turn:{t.turn_id}:{n}" for n in range(len(t.results)))
    return refs


# Every image of a chat (upload or result) gets a number when it's made, never reused:
# "#7". Messages refer to earlier images by it ("hair like #3").
# Up to 4 digits: "#222222" is a color, not image 222222.
_MENTION = re.compile(r"(?<![\w#])#(\d{1,4})(?![\w])")
# Quoted text is lettering for the image ("Room #3"), never a reference.
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")


def sub_mentions(text: str, repl: Any, pattern: re.Pattern[str] = _MENTION) -> str:
    """Replace image mentions (#3; with image_ids.MENTION, global ones: @1234) outside
    quotes with repl(number) (a str, or None to leave the mention as it is)."""

    def one(m: re.Match[str]) -> str:
        r = repl(int(m[1]))
        return r if r is not None else m[0]

    def outside(chunk: str) -> str:
        return pattern.sub(one, chunk)

    out, at = [], 0
    for q in _QUOTED.finditer(text):
        out += [outside(text[at : q.start()]), q[0]]
        at = q.end()
    out.append(outside(text[at:]))
    return "".join(out)


def global_mentions(text: str) -> list[int]:
    """Global image numbers a message mentions (@1234), in order of first mention."""
    seen: list[int] = []
    sub_mentions(text, lambda n: seen.append(n) if n not in seen else None, GLOBAL_MENTION)
    return seen


def chat_text(text: str, numbers: dict[str, int]) -> str:
    """A message with each global number it mentions (@1234) as the chat number that image
    came in as (#5): how everything past the API reads it."""
    return sub_mentions(
        text, lambda g: f"#{numbers[str(g)]}" if str(g) in numbers else None, GLOBAL_MENTION
    )


def image_labels(turns: list[ExpTurn]) -> dict[int, str]:
    """Image number -> reference string, for every numbered image of a chat."""
    out: dict[int, str] = {}
    for t in turns:
        for a, label in zip(t.attachments, t.attachment_labels, strict=False):
            out[label] = f"asset:{a}"
        for n, r in enumerate(t.results):
            if r.label is not None:
                out[r.label] = f"turn:{t.turn_id}:{n}"
    return out


def ref_label(ref: str, turns: list[ExpTurn]) -> int | None:
    return next((k for k, v in image_labels(turns).items() if v == ref), None)


def turn_label(turn: ExpTurn, ref: str, turns: list[ExpTurn]) -> int | None:
    """A picture's number, including this turn's own attachments."""
    others = [t for t in turns if t.turn_id != turn.turn_id]
    return ref_label(ref, [*others, turn])


def mentions(text: str) -> list[int]:
    """Image numbers a message mentions (#3), in order of first mention."""
    seen: list[int] = []

    def note(n: int) -> None:
        if n not in seen:
            seen.append(n)

    sub_mentions(text, note)
    return seen


def unlabeled(turns: list[ExpTurn]) -> int:
    """Images made before numbering existed (to number once, oldest first)."""
    return sum(
        max(0, len(t.attachments) - len(t.attachment_labels))
        + sum(1 for r in t.results if r.label is None)
        for t in turns
    )


def apply_labels(turns: list[ExpTurn], labels: list[int]) -> list[ExpTurn]:
    """Give unnumbered images the given numbers, oldest first; returns the changed turns."""
    it = iter(labels)
    changed: list[ExpTurn] = []
    for t in sorted(turns, key=lambda t: t.index):
        before = t.model_dump()
        while len(t.attachment_labels) < len(t.attachments):
            t.attachment_labels.append(next(it))
        for r in t.results:
            if r.label is None:
                r.label = next(it)
        if t.model_dump() != before:
            changed.append(t)
    return changed


def size_of(dims: str | None) -> str:
    """The chat size nearest a picture's "WIDTHxHEIGHT" ("" when unknown): what an
    automatic size takes from a copied-in picture (a pick start, an image brought in)."""
    w, _, h = (dims or "").partition("x")
    return nearest_size(int(w), int(h)) if w.isdigit() and h.isdigit() else ""


def nearest_size(width: int | None, height: int | None) -> str:
    if not width or not height:
        return DEFAULT_SIZE
    want = math.log(width / height)

    def gap(size: str) -> float:
        w, h = (int(x) for x in size.split("x"))
        return abs(math.log(w / h) - want)

    return min(SIZES.values(), key=gap)


def plan_turn(
    session: ExpSession,
    turns: list[ExpTurn],
    assets: dict[str, dict[str, Any]],
    *,
    text: str,
    attachments: list[str],
    mode: Literal["rewrite", "exact"],
    draws: int,
    size: str | None,
    restrictive: bool = False,
    numbers: dict[int, int] | None = None,
) -> ExpTurn:
    """The next turn of a session (not saved). `numbers`: the chat number (#5) of each
    global number the message mentions (@1234), from images brought in before it.
    Raises ExpError."""
    text = text.strip()
    by_gid = {str(g): n for g, n in (numbers or {}).items()}
    if unknown := [g for g in global_mentions(text) if str(g) not in by_gid]:
        raise ExpError(f"There's no image {', '.join(f'@{g}' for g in unknown)}.")
    if is_busy(turns):
        raise ExpError("Wait for the current image to finish first.")
    if not text and not attachments:
        raise ExpError("Write a message or attach an image.")
    for a in attachments:
        raw = assets.get(a)
        if raw is None:
            raise ExpError(f"unknown upload {a}")
        if raw.get("kind") != "image":
            raise ExpError("Only images can be attached here.")
    index = max((t.index for t in turns), default=0) + 1
    turn = ExpTurn(
        turn_id=f"turn_{uuid.uuid4().hex[:12]}",
        session_id=session.session_id,
        index=index,
        text=text,
        attachments=attachments,
        draws=max(1, min(draws, MAX_DRAWS)),
        restrictive=restrictive,
        global_mentions={g: by_gid[g] for g in map(str, global_mentions(text))},
    )
    local = chat_text(text, turn.global_mentions)
    if not text:
        turn.mode, turn.status, turn.finished_at = "upload", "done", utcnow()
        turn.references = [f"asset:{a}" for a in attachments]
        return turn
    # Pictures: the current image, then earlier images the message mentions (#3), then this
    # message's attachments. (In "rewrite" mode the writer may reorder or drop them, e.g.
    # "go back to #3" makes #3 the base.)
    labels = image_labels(turns)
    if unknown := [n for n in mentions(local) if n not in labels]:
        raise ExpError(f"There's no image {', '.join(f'#{n}' for n in unknown)} in this chat.")
    refs = [session.current] if session.current else []
    refs += [labels[n] for n in mentions(local) if labels[n] not in refs]
    refs += [f"asset:{a}" for a in attachments if f"asset:{a}" not in refs]
    if len(refs) > MAX_REFERENCES:
        raise ExpError(
            f"At most {MAX_REFERENCES} pictures per message, the current image included."
        )
    turn.mode, turn.references, turn.offered = mode, refs, list(refs)
    turn.from_upload = any(_from_upload(r, turns) for r in refs)
    if mode == "exact":
        # "#3" in an exact prompt becomes the picture it is sent as.
        prompt = sub_mentions(local, lambda n: f"<Picture {refs.index(labels[n]) + 1}>")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ExpError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters.")
        turn.prompt = prompt
        if missing := cited_beyond(prompt, len(refs)):
            raise ExpError(
                f"The prompt cites {', '.join(missing)}, but this message sends "
                f"{len(refs)} picture{'s' if len(refs) != 1 else ''}."
            )
    turn.auto_size = (size or session.size) == "auto"
    turn.size = _size_for(size or session.size, refs, turns, assets)
    return turn


def _from_upload(ref: str, turns: list[ExpTurn]) -> bool:
    """Whether a picture is an upload or was made, at any remove, from one. Follows the
    saved references (not the stored from_upload flag, which turns saved before it existed
    lack). An unknown turn counts as from an upload."""
    by_id = {t.turn_id: t for t in turns}
    todo, seen = [ref], set()
    while todo:
        kind, ident, _ = parse_ref(todo.pop())
        if kind == "asset":
            return True
        if ident in seen:
            continue
        seen.add(ident)
        made = by_id.get(ident)
        if made is None:
            return True
        if made.mode == "import":  # its lineage lies outside this chat
            if made.from_upload:
                return True
            continue
        todo.extend(made.references)
        todo.extend(made.offered)
    return False


def likeness_of(ref: str, turns: list[ExpTurn]) -> bool | None:
    """Whether a picture shows a real person's likeness: True/False for results judged when
    they were made, None for uploads (the model judges who is in them) and older results."""
    kind, ident, _ = parse_ref(ref)
    if kind == "asset":
        return None
    made = next((t for t in turns if t.turn_id == ident), None)
    return made.likeness if made is not None else None


def picture_kind(ref: str, turns: list[ExpTurn]) -> str:
    """How the content rules see a picture (see content_guard rule 3)."""
    kind, _, _ = parse_ref(ref)
    if kind == "asset":
        return (
            "uploaded photo (judge whether it shows an identifiable real person, and whether "
            "this message takes their likeness or only clothing, a place or an object)"
        )
    made = next((t for t in turns if ref.startswith(f"turn:{t.turn_id}:")), None)
    if made is not None and made.mode == "pick" and made.likeness is None:
        return (
            "the story's design picture, from a story with uploaded photos: judge from the "
            "picture and the prompt that made it whether it shows a real person's likeness"
        )
    if made is not None and made.mode == "import":
        match made.likeness:
            case True:
                return "brought in from elsewhere in the app; shows a real person's likeness"
            case False:
                return "brought in from elsewhere in the app; no real person's likeness"
        return (
            "brought in from elsewhere in the app (or an upload): judge from the picture and "
            "the prompt that made it whether it shows a real person's likeness"
        )
    match likeness_of(ref, turns):
        case True:
            return "made in this chat; shows a real person's likeness"
        case False:
            return "made in this chat; no real person's likeness (fictional)"
    return (
        "made in this chat before likeness was tracked: judge from the picture and the "
        "prompt that made it whether it shows a real person's likeness"
    )


ANCESTRY_STEPS = 30
ANCESTRY_PROMPT_CHARS = 800


def ancestry(turn: ExpTurn, by_id: dict[str, ExpTurn]) -> list[dict[str, str]]:
    """The messages and prompts an image was made from, back to where its chain started
    (oldest first): what a likeness judgment needs when none was stored, however far back
    a real person was named."""
    seen: set[str] = set()
    chain: list[ExpTurn] = []
    todo = [turn]
    while todo and len(chain) < ANCESTRY_STEPS:
        t = todo.pop()
        if t.turn_id in seen:
            continue
        seen.add(t.turn_id)
        chain.append(t)
        for ref in [*t.references, *t.offered]:
            kind, ident, _ = parse_ref(ref)
            if kind == "turn" and ident in by_id:
                todo.append(by_id[ident])
    return [
        {"message": t.text, "prompt": t.prompt[:ANCESTRY_PROMPT_CHARS]}
        for t in sorted(chain, key=lambda t: t.index)
        if t.mode != "upload"
    ]


def inherited_likeness(turn: ExpTurn, turns: list[ExpTurn]) -> bool:
    """The floor under the model's own judgment: a result keeps the likeness of the picture
    it changes (<Picture 1>), and a result made from no picture after the writer looked at
    one with a likeness keeps it too (it may describe that person)."""
    if turn.references:
        return likeness_of(turn.references[0], turns) is True
    return any(likeness_of(r, turns) is True for r in turn.offered)


def _size_for(
    size: str, refs: list[str], turns: list[ExpTurn], assets: dict[str, dict[str, Any]]
) -> str:
    if size != "auto":
        size = current_size(size)
        if size not in SIZES.values():
            raise ExpError(f"unsupported size {size}")
        return size
    if not refs:
        return DEFAULT_SIZE
    kind, ident, _ = parse_ref(refs[0])
    if kind == "turn":
        made = next((t for t in turns if t.turn_id == ident), None)
        return current_size(made.size) if made and made.size else DEFAULT_SIZE
    raw = assets.get(ident) or {}
    return nearest_size(raw.get("width"), raw.get("height"))


_PICTURE = re.compile(r"<Picture (\d+)>")


def cited_beyond(prompt: str, n: int) -> list[str]:
    """<Picture N> tokens the prompt cites that the turn doesn't send."""
    over = sorted({int(m) for m in _PICTURE.findall(prompt) if not 1 <= int(m) <= n})
    return [f"<Picture {m}>" for m in over]


# ---------------------------------------------------------------------------
# Prompt writer

WRITER_ROLE = """\
You are the prompt writer in an image-editing chat. The user talks to you in plain language \
("make her look younger", "put the pencil in a bun at the back", "same woman, rainy street at \
night") and you turn each message into one finished prompt for MiniMax-H3-Image. Our server \
renders the prompt exactly as written, with no prompt expansion, so the prompt must be the \
complete description of the finished image. Follow the rules and examples below exactly.\
"""

WRITER_GUIDE = """\
## Your input

- `message`: what the user wants now.
- `pictures`: the pictures offered with this message, each attached to this request right \
after its label, so look at them. Each has an `id` (#7): every image in the chat has a \
number, and the user refers to earlier images by it ("hair like #3", "go back to #5"). By \
default <Picture 1> is the current image (the one the user is changing), then the earlier \
images the message mentions, then images attached to this message. `made_by_prompt` is the \
prompt that made a picture, when it came from this chat.
- `current_image_prompt`: the prompt that made the current image, when it came from this chat. \
Treat it as the description of <Picture 1>; it does not need to be repeated.
- `history`: earlier messages and the prompts written for them, oldest first, for context \
("again", "undo that", "back to the earlier hair").
- `size` and `aspect`: the output canvas.
- `draws`: how many images the user's Draws setting renders from your prompt, each with its \
own random seed.

## Draws

A draw is one separate render of your prompt with a new seed. When the message asks for a \
number of separate images ("do 4 draws", "give me three versions", "show me a few options"), \
return that number in `draws` (1 to {max_draws}; for more, return {max_draws} and say so in \
`reply`; for "a few" or "some", return 3) and write the prompt for ONE image: every draw \
renders the same prompt. Otherwise return null and the user's setting stays. Never show \
alternatives as a grid, collage, comparison sheet or panels within one image unless the user \
asks for one image laid out that way (e.g. "a turnaround sheet"). Draws differ only by seed, \
so an edit that preserves most of <Picture 1> gives close results; if the user wants clearly \
different designs, say in `reply` that one message per design gives more variety.

## How to write it

- With pictures: write an edit prompt, one paragraph, in the order the rules give: define each \
picture as a <Subject N> (only the scopes that need their own relationship), then "The target \
image ...", then exactly one relationship per subject, then the changed details. Preserve \
everything the user did not ask to change: name it as preserved (identity, face, pose, \
wardrobe, background, framing, light), and describe only what changes, concretely and as the \
finished image ("her hair is gathered into a low bun"), never as a command ("change her hair"). \
End with "No other lettering is added." unless the user wants text in the image.
- Without pictures: write a text-to-image prompt (what the image is, layout, details, light) and \
end with "The image contains no text, letters or numbers." unless the user wants text.
- Turn vague wishes into visible details (not "more professional" but what makes it look so). \
Keep names as the user wrote them. Write in English; quote any lettering exactly.
- Never cite a picture that is not listed. Never write "#7" in the prompt: the image model \
only knows <Picture N>.
- Choosing the pictures: return `pictures`, the ids to send, in <Picture N> order. Put the \
image to change first: usually the current image, but the mentioned image when the user wants \
to start from it ("go back to #3 and add a hat": just #3; "like #3 but with #5's jacket": \
#3 then #5). Leave out pictures the result doesn't need. Number the prompt's <Picture N> by \
your list. Return null to send them as offered.

## What may be drawn

Each picture has a `kind`. These rules decide what you may write a prompt for:

{rules}

{mode}

Also return `real_person`: whether the person in the image you're asking for has a real \
person's likeness (rule 3): kept from a picture that has one, taken from an identifiable \
person in an uploaded photo (anyone whose face is visible in one counts), or because the \
message makes someone resemble a named or described real person. An uploaded photo used only \
for its clothing, a place or an object gives no likeness. For a picture made before likeness \
was tracked, read its `ancestry` (the messages and prompts it was made from, oldest first).

If the request breaks a rule, don't write a prompt: return allowed=false, an empty prompt, and \
in `refusal` one short sentence to the user naming the rule. Don't write a toned-down version \
of a refused request.

## Output

Return {{"allowed": true, "real_person": false, "pictures": ["#3", ...] or null, "draws": 4 or null, \
"prompt": ..., "reply": ..., "refusal": ""}}: `prompt` is the prompt only, as plain text; `reply` is one or two short sentences to the user saying what you \
changed or how you read the request (no prompt text in it).\
"""


class ExpPromptOutput(BaseModel):
    allowed: bool = True
    # Required: an answer without it is a failed rewrite (the message then goes through
    # the content check), never a silent "no likeness".
    real_person: bool
    # Ids of the pictures to send, in <Picture N> order; None: as offered.
    pictures: list[str] | None = None
    # Separate renders the message asks for ("do 4 draws"); None: keep the user's setting.
    draws: int | None = None
    prompt: str = ""
    reply: str = ""
    refusal: str = ""


def writer_instructions(restrictive: bool = False, rating: str = DEFAULT_RATING) -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    guide = WRITER_GUIDE.format(
        rules=content_rules(rating), mode=mode_rule(restrictive), max_draws=MAX_DRAWS
    )
    parts = [WRITER_ROLE, load_h3_image_rules(), load_h3_image_examples(), guide]
    return "\n\n".join(parts)


def fixture_prompt(request: GenerationRequest) -> ExpPromptOutput:
    """Offline stand-in for the writer: a plain prompt built from the message."""
    ctx = request.context
    n = len(ctx.get("pictures") or [])
    msg = ctx.get("message", "")
    if n:
        defs = " ".join(f"<Picture {i}> is <Subject {i}>." for i in range(1, n + 1))
        prompt = (
            f"{defs} The target image is <Subject 1> with this change: {msg}. Everything else "
            "in <Subject 1> stays as it is. No other lettering is added."
        )
    else:
        prompt = f"A photograph: {msg}. The image contains no text, letters or numbers."
    return ExpPromptOutput(
        real_person=False, prompt=prompt, reply="Offline sample: your message, lightly wrapped."
    )


def _aspect(size: str) -> str:
    return next((k for k, v in SIZES.items() if v == size), size)


# ---------------------------------------------------------------------------
# Runner


class ExperimentRunner:
    """Runs turns in the API process, one render at a time (the GPU makes one image at a
    time anyway). Unfinished turns don't survive a restart: `recover` marks them failed."""

    def __init__(
        self,
        repo: Any,
        media: MediaStore,
        images: ImageClient,
        writer_client: Any,
        providers: dict[str, ImageClient] | None = None,
    ) -> None:
        self.repo = repo
        self.media = media
        self.images = images
        self.writer_client = writer_client
        # Providers besides H3 (`images`) a turn may name: "nano-banana", "local".
        self.providers = dict(providers or {})
        self._gpu = asyncio.Lock()
        # One render at a time per provider; H3 shares the GPU lock with keyframes.
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def recover(self) -> int:
        return self.repo.fail_unfinished_exp_turns(
            "Interrupted by a server restart. Retry to run it again."
        )

    def start(self, turn_id: str) -> None:
        task = asyncio.create_task(self.run(turn_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Providers besides H3 are this runner's own (H3 is closed by the app).
        for client in self.providers.values():
            if close := getattr(client, "aclose", None):
                await close()

    async def run(self, turn_id: str) -> None:
        turn = self.repo.get_exp_turn(turn_id)
        if turn is None or turn.status != "pending":
            return
        turn.status = "running"
        self.repo.save_exp_turn(turn)
        try:
            if turn.mode == "describe":
                await self._describe(turn)
            elif not turn.prompt:
                await self._write_prompt(turn)  # may choose which pictures to send
                self.repo.save_exp_turn(turn)
            await self._guard(turn)
            # Read in a thread: up to nine images of several MB each.
            refs = await asyncio.to_thread(self._references, turn) if turn.draws else []
            client, lock = self._client(turn.provider)
            for n in range(len(turn.results), turn.draws):
                seed = turn.seeds[n] if n < len(turn.seeds) else random.randint(1, 2**31 - 1)
                async with lock:
                    result = await client.generate(turn.prompt, current_size(turn.size), seed, refs)
                ext = "png" if result.content_type == "image/png" else "jpg"
                rel = f"experiments/{turn.session_id}/{turn.turn_id}_{n}.{ext}"
                await asyncio.to_thread(self._write, rel, result.data)
                from .images import make_thumb

                thumb = await asyncio.to_thread(make_thumb, self.media, rel)
                [label] = self.repo.allocate_exp_labels(turn.session_id, 1)
                turn.results.append(
                    ExpResult(
                        label=label,
                        file=rel,
                        thumb=thumb,
                        content_type=result.content_type,
                        seed=seed,
                    )
                )
                self.repo.save_exp_turn(turn)  # each draw shows as soon as it's done
        except asyncio.CancelledError:
            turn.status, turn.error = (
                "failed",
                "Stopped (the server shut down). Retry to run it again.",
            )
            turn.finished_at = utcnow()
            self.repo.save_exp_turn(turn)
            raise
        except GuardRefused as exc:
            turn.status, turn.error, turn.finished_at = "failed", str(exc), utcnow()
            turn.refused = True
            self.repo.save_exp_turn(turn)
            return
        except Exception as exc:
            log.warning("experiment turn %s failed", turn_id, exc_info=True)
            turn.status, turn.error, turn.finished_at = (
                "failed",
                str(exc) or type(exc).__name__,
                utcnow(),
            )
            self.repo.save_exp_turn(turn)
            return
        turn.status, turn.error, turn.finished_at = "done", None, utcnow()
        self.repo.save_exp_turn(turn)
        session = self.repo.get_exp_session(turn.session_id)
        if session is not None:
            if session.kind == "chat" and turn.results:
                session.current = f"turn:{turn.turn_id}:0"
            session.updated_at = utcnow()
            self.repo.save_exp_session(session)

    def _client(self, provider: str | None) -> tuple[ImageClient, asyncio.Lock]:
        """The image client a turn names (None: H3) and the lock it renders under."""
        if not provider:
            return self.images, self._gpu
        client = self.providers.get(provider)
        if client is None:
            raise ImageGenError(f"the image provider {provider!r} isn't configured")
        return client, self._locks.setdefault(provider, asyncio.Lock())

    def _references(self, turn: ExpTurn) -> list[Reference]:
        """The turn's pictures for H3 in <Picture N> order, read from disk (blocking).

        Uploads go as uploaded, so a base image keeps its full detail, when they are JPEG
        or PNG and all pictures fit H3's 24 MiB budget; otherwise as the downscaled copy."""
        files = [self._picture(turn, i, "full") for i in range(len(turn.references))]
        if sum(p.stat().st_size for p, _ in files) > MAX_REFERENCE_BYTES:
            files = [self._picture(turn, i, "model") for i in range(len(turn.references))]
        return [Reference(p.name, p.read_bytes(), mime) for p, mime in files]

    def _picture(self, turn: ExpTurn, i: int, variant: PictureVariant) -> tuple[Path, str]:
        path, mime = self.ref_file(turn.references[i], variant=variant)
        if path is None:
            raise ExpError(f"<Picture {i + 1}> ({turn.references[i]}) is no longer available.")
        return path, mime

    def ref_file(self, ref: str, *, variant: PictureVariant) -> tuple[Path | None, str]:
        """A picture's file. "full": an upload as uploaded (JPEG/PNG) and a result as
        rendered; "model": an upload's downscaled JPEG copy, a result as rendered;
        "thumb": small copies, for showing the text model."""
        kind, ident, n = parse_ref(ref)
        if kind == "asset":
            raw = self.repo.get_source_asset(ident)
            if not raw:
                return None, ""
            asset = SourceAsset.model_validate(raw)
            as_uploaded = asset.content_type in ("image/jpeg", "image/png")
            if asset.model_image and (variant != "full" or not as_uploaded):
                path, mime = self.media.path(asset.model_image), "image/jpeg"
            else:
                path, mime = self.media.path(asset.file), asset.content_type
        else:
            made = self.repo.get_exp_turn(ident)
            if made is None or n >= len(made.results):
                return None, ""
            r = made.results[n]
            if variant == "thumb" and r.thumb:
                path, mime = self.media.path(r.thumb), "image/jpeg"
            else:
                path, mime = self.media.path(r.file), r.content_type
        return (path, mime) if path.is_file() else (None, "")

    def _for_model(
        self, turn: ExpTurn
    ) -> tuple[list[dict[str, str]], list[MediaPart], str | None, list[ExpTurn]]:
        """What the text model is told about the pictures: (one entry per <Picture N>, the
        pictures as small images, the prompt that made the current image, earlier turns)."""
        turns = [t for t in self.repo.list_exp_turns(turn.session_id) if t.index < turn.index]
        made = {t.turn_id: t for t in turns}
        parts: list[dict[str, str]] = []
        media: list[MediaPart] = []
        current_prompt = None
        for i, ref in enumerate(turn.references, 1):
            kind, ident, _ = parse_ref(ref)
            if kind == "turn" and ident in made and made[ident].mode == "import":
                what = made[ident].text.removesuffix(".")
            elif kind == "turn":
                what = (
                    f"an image made in this chat (message {made[ident].index})"
                    if ident in made
                    else "an image made in this chat"
                )
                if i == 1 and ident in made:
                    current_prompt = made[ident].prompt
            elif ident in turn.attachments:
                what = "an image the user attached to this message"
            else:
                what = "an image the user uploaded earlier"
            if i == 1:
                what = f"the current image: {what}"
            label = turn_label(turn, ref, turns)
            part = {
                "picture": f"<Picture {i}>",
                "id": f"#{label}" if label is not None else "",
                "what": what,
                "kind": picture_kind(ref, turns),
            }
            if kind == "turn" and ident in made:
                part["made_by_prompt"] = made[ident].prompt
                if made[ident].likeness is None:  # older: let the model read where it came from
                    part["ancestry"] = ancestry(made[ident], made)
            parts.append(part)
            path, mime = self.ref_file(ref, variant="thumb")  # the model only needs to see it
            if path is not None:
                media.append(MediaPart(label=f"<Picture {i}>", path=path, mime=mime))
        return parts, media, current_prompt, turns

    async def _describe(self, turn: ExpTurn) -> None:
        """Face Lab: the text model writes a prompt recreating the photo (nothing drawn)."""
        from .face_lab import STAGE as DESCRIBE_STAGE
        from .face_lab import DescribeOutput, describe_instructions

        if reason := text_guard(turn.text, turn.rating):
            raise GuardRefused(reason)
        asset = turn.attachments[0]
        # The model copy (up to 1,536 px) shows the face in detail; thumbs are too small.
        path, mime = self.ref_file(f"asset:{asset}", variant="model")
        if path is None:
            raise ExpError("The photo is no longer available.")
        previous = next(
            (
                t.prompt
                for t in sorted(self.repo.list_exp_turns(turn.session_id), key=lambda t: -t.index)
                # Only this photo's: another person's description would blend in.
                if t.mode == "describe"
                and t.index < turn.index
                and t.prompt
                and t.attachments == turn.attachments
            ),
            None,
        )
        session = self.repo.get_exp_session(turn.session_id)
        subject = session.subject if session is not None else "face"
        media = [MediaPart(label="<Photo>", path=path, mime=mime)]
        # A study whose tests are drawn from <Picture 1> (the face, a first state): the
        # writer sees it too, to write the edit around it.
        anchored = False
        if turn.references:
            a_path, a_mime = self.ref_file(turn.references[0], variant="model")
            if a_path is not None:
                media.append(MediaPart(label="<Picture 1>", path=a_path, mime=a_mime))
                anchored = True
        request = GenerationRequest(
            stage=DESCRIBE_STAGE,  # type: ignore[arg-type]
            instructions=describe_instructions(turn.restrictive, turn.rating, subject, anchored),
            context={
                "hint": turn.text or None,
                "previous_prompt": previous,
                "subject": subject,
                "anchored": anchored,
                "design": self._design_of(session),
            },
            media=media,
        )
        out = await self.writer_client.generate(request, DescribeOutput)
        if not out.allowed:
            raise GuardRefused(out.refusal.strip() or "This photo can't be described here.")
        prompt = out.prompt.strip()
        if not prompt:
            raise ExpError("The description came back empty. Try again.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ExpError(f"The description is longer than {MAX_PROMPT_CHARS} characters.")
        if cited := cited_beyond(prompt, 1 if anchored else 0):  # copied from the examples
            raise ExpError(
                f"The description cites {', '.join(cited)}, but these tests send "
                f"{'only <Picture 1>' if anchored else 'no pictures'}. Retry for a new "
                "description."
            )
        if reason := text_guard(prompt, turn.rating):
            raise GuardRefused(reason)
        turn.prompt, turn.reply = prompt, out.notes.strip()
        turn.prompt_model = request.model or "unknown"
        # A photo copied from a picture marked as a likeness stays one (planned as True);
        # so does a description drawn on a face marked as one (<Picture 1>).
        turn.likeness = out.real_person or turn.likeness is True

    def _design_of(self, session: Any) -> dict[str, Any] | None:
        """What the story says the study's picture must show (its requirement's design
        text), for the writer."""
        t = session.target if session is not None else None
        if t is None or t.keyframe:
            return None
        pkg = self.repo.get_package(t.package_id)
        req = pkg and next((r for r in pkg.asset_requirements.items
                            if r.requirement_id == t.requirement_id), None)  # fmt: skip
        if req is None:
            return None
        return {"brief": req.design_brief, "must_keep": req.must_keep}

    async def _guard(self, turn: ExpTurn) -> None:
        """Raise GuardRefused unless the turn's prompt may be drawn. A prompt the writer
        wrote was judged by the writer (it sees the rules and the pictures); one sent as
        typed (exact mode, or the writer failed) is screened by the text model."""
        if reason := text_guard(turn.prompt, turn.rating) or text_guard(turn.text, turn.rating):
            raise GuardRefused(reason)
        if turn.prompt_model is None:
            parts, media, _, turns = self._for_model(turn)
            verdict = await screen(
                self.writer_client,
                turn.prompt,
                pictures=parts,
                media=media,
                message=turn.text if turn.text != turn.prompt else None,
                restrictive=turn.restrictive,
                rating=turn.rating,
            )
            # Only ever adds a likeness (the writer may already have found one).
            turn.likeness = (
                turn.likeness is True or verdict.real_person or inherited_likeness(turn, turns)
            )

    async def _write_prompt(self, turn: ExpTurn) -> None:
        if reason := text_guard(turn.text, turn.rating):  # clear cases need no model call
            raise GuardRefused(reason)
        parts, media, current_prompt, turns = self._for_model(turn)
        history = [
            {"message": chat_text(t.text, t.global_mentions), "prompt": t.prompt}
            for t in turns
            if t.mode != "upload" and t.status == "done"
        ][-HISTORY_TURNS:]
        request = GenerationRequest(
            stage=STAGE,  # type: ignore[arg-type]
            instructions=writer_instructions(turn.restrictive, turn.rating),
            context={
                "message": chat_text(turn.text, turn.global_mentions),
                "pictures": parts,
                "current_image_prompt": current_prompt,
                "history": history,
                "size": turn.size,
                "aspect": _aspect(turn.size),
                "draws": turn.draws,
            },
            media=media,
        )
        try:
            out = await self.writer_client.generate(request, ExpPromptOutput)
        except Exception as exc:
            log.warning("experiment prompt writer failed", exc_info=True)
            self._send_as_written(turn, exc)
            return
        if not out.allowed:
            raise GuardRefused(out.refusal.strip() or "This image isn't allowed here.")
        offered = list(turn.references)
        if out.pictures is not None:
            chosen = self._choose(turn, out.pictures, turns)
            if chosen is None:
                self._send_as_written(turn, ValueError(f"unknown pictures {out.pictures}"))
                return
            self._set_references(turn, chosen, turns)
        prompt = out.prompt.strip()
        problem = (
            "the writer returned an empty prompt"
            if not prompt
            else f"the prompt cites {', '.join(missing)}, which wasn't sent"
            if (missing := cited_beyond(prompt, len(turn.references)))
            # Cutting it could drop the closing sentences.
            else f"the prompt is longer than {MAX_PROMPT_CHARS} characters"
            if len(prompt) > MAX_PROMPT_CHARS
            else None
        )
        if problem:
            self._set_references(turn, offered, turns)  # the writer's choice goes with it
            self._send_as_written(turn, ValueError(problem))
            return
        turn.prompt = prompt
        if out.draws is not None and not turn.seeds:  # fixed seeds set their own count
            turn.draws = max(1, min(out.draws, MAX_DRAWS))
        turn.reply = out.reply.strip()
        turn.prompt_model = request.model
        turn.likeness = out.real_person or inherited_likeness(turn, turns)

    def _choose(self, turn: ExpTurn, ids: list[str], turns: list[ExpTurn]) -> list[str] | None:
        """The references for the writer's picture ids, or None if one wasn't offered."""
        offered = {f"#{turn_label(turn, r, turns)}": r for r in turn.references}
        chosen = [offered.get(i.strip()) for i in ids]
        if any(c is None for c in chosen) or len(set(ids)) != len(ids):
            return None
        return [c for c in chosen if c is not None]

    def _set_references(self, turn: ExpTurn, refs: list[str], turns: list[ExpTurn]) -> None:
        """Send these pictures instead; lineage and an automatic size follow."""
        if refs == turn.references:
            return
        turn.references = refs
        # What the writer saw counts too (see ExpTurn.offered).
        turn.from_upload = any(_from_upload(r, turns) for r in [*refs, *turn.offered])
        if turn.auto_size:
            assets = self.repo.get_source_assets(
                [r.removeprefix("asset:") for r in refs if r.startswith("asset:")]
            )
            turn.size = _size_for("auto", refs, turns, assets)

    def _send_as_written(self, turn: ExpTurn, exc: Exception) -> None:
        """Fallback when the writer fails: the message is the prompt, with the same checks
        as an exact prompt (prompt_model stays None, so _guard screens it). "#3" becomes the
        picture it is sent as."""
        turns = self.repo.list_exp_turns(turn.session_id)

        def picture(n: int) -> str | None:
            for i, ref in enumerate(turn.references, 1):
                if turn_label(turn, ref, turns) == n:
                    return f"<Picture {i}>"
            return None

        text = sub_mentions(chat_text(turn.text, turn.global_mentions), picture)
        if missing := cited_beyond(text, len(turn.references)):
            raise ExpError(
                f"Couldn't rewrite your message ({exc}), and as written it cites "
                f"{', '.join(missing)}, which this message doesn't send."
            ) from exc
        turn.prompt = text
        turn.reply = (
            f"Couldn't rewrite your message ({exc}); sent it to the image model as written."
        )

    def _write(self, rel: str, data: bytes) -> None:
        path = self.media.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete_files(self, session_id: str, uploads: list[str]) -> None:
        """Remove a deleted chat's results, and its uploads that nothing else uses
        (another chat, a job, or a reference image). Call after deleting the chat."""
        with contextlib.suppress(OSError):
            shutil.rmtree(self.media.path(f"experiments/{session_id}"))
        for asset_id in uploads:
            if not self.repo.asset_in_use(asset_id):
                self.media.delete(asset_id)
                self.repo.delete_source_asset(asset_id)
