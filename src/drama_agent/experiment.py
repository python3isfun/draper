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

from .content_guard import RULES as CONTENT_RULES
from .content_guard import GuardRefused, screen, text_guard
from .imagegen import MAX_REFERENCE_BYTES, MAX_REFERENCES, ImageClient, Reference
from .llm import GenerationRequest
from .media import MediaPart, MediaStore
from .models import SourceAsset
from .models.package import utcnow

log = logging.getLogger(__name__)

STAGE = "experiment"
MAX_PROMPT_CHARS = 6000  # H3 reads at most ~4,096 tokens
MAX_DRAWS = 4
HISTORY_TURNS = 8  # earlier messages shown to the prompt writer

# H3 sizes at about 16 MP (both sides multiples of 32), by aspect ratio.
SIZES: dict[str, str] = {
    "1:1": "4096x4096",
    "3:4": "3552x4736",
    "4:3": "4736x3552",
    "2:3": "3360x5024",
    "3:2": "5024x3360",
    "9:16": "3072x5472",
    "16:9": "5472x3072",
    "21:9": "6272x2688",
}
DEFAULT_SIZE = SIZES["1:1"]

TurnStatus = Literal["pending", "running", "done", "failed"]
TurnMode = Literal["rewrite", "exact", "upload"]
PictureVariant = Literal["full", "model", "thumb"]


class ExpResult(BaseModel):
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
    # The pictures sent, in <Picture N> order: "asset:<id>" or "turn:<turn_id>:<n>".
    references: list[str] = Field(default_factory=list)
    mode: TurnMode = "rewrite"
    size: str = ""
    draws: int = 1
    prompt: str = ""  # what H3 received
    prompt_model: str | None = None  # the text model that wrote it ("rewrite")
    reply: str = ""  # the writer's short note on what it changed
    status: TurnStatus = "pending"
    error: str | None = None
    refused: bool = False  # the content guard refused it (retrying won't help)
    # Any picture is an upload or was made from one: its results may show a real person.
    from_upload: bool = False
    results: list[ExpResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class ExpSession(BaseModel):
    session_id: str
    title: str = "New chat"
    size: str = "auto"  # "auto": the current image's aspect ratio
    current: str | None = None  # the image the next message changes (a reference string)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExpError(ValueError):
    """A request the session can't take (shown to the user)."""


def new_session(title: str | None = None) -> ExpSession:
    return ExpSession(session_id=f"exp_{uuid.uuid4().hex[:12]}", title=title or "New chat")


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
) -> ExpTurn:
    """The next turn of a session (not saved). Raises ExpError."""
    text = text.strip()
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
    )
    if not text:
        turn.mode, turn.status, turn.finished_at = "upload", "done", utcnow()
        turn.references = [f"asset:{a}" for a in attachments]
        return turn
    refs = ([session.current] if session.current else []) + [f"asset:{a}" for a in attachments]
    if len(refs) > MAX_REFERENCES:
        raise ExpError(
            f"At most {MAX_REFERENCES} pictures per message, the current image included."
        )
    turn.mode, turn.references = mode, refs
    turn.from_upload = any(_from_upload(r, turns) for r in refs)
    if mode == "exact":
        if len(text) > MAX_PROMPT_CHARS:
            raise ExpError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters.")
        turn.prompt = text
        if missing := cited_beyond(text, len(refs)):
            raise ExpError(
                f"The prompt cites {', '.join(missing)}, but this message sends "
                f"{len(refs)} picture{'s' if len(refs) != 1 else ''}."
            )
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
        todo.extend(made.references)
    return False


def picture_kind(ref: str, turns: list[ExpTurn]) -> str:
    """How the content rules see a picture (see content_guard rule 3)."""
    kind, _, _ = parse_ref(ref)
    if kind == "asset":
        return "uploaded photo"
    if _from_upload(ref, turns):
        return "uploaded photo, as edited in this chat (may show a real person)"
    return "generated image (made in this chat from text only)"


def _size_for(
    size: str, refs: list[str], turns: list[ExpTurn], assets: dict[str, dict[str, Any]]
) -> str:
    if size != "auto":
        if size not in SIZES.values():
            raise ExpError(f"unsupported size {size}")
        return size
    if not refs:
        return DEFAULT_SIZE
    kind, ident, _ = parse_ref(refs[0])
    if kind == "turn":
        made = next((t for t in turns if t.turn_id == ident), None)
        return made.size if made and made.size else DEFAULT_SIZE
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
- `pictures`: the pictures this message sends, in order, as <Picture 1>, <Picture 2>, ... \
Each is attached to this request right after its label, so look at them. <Picture 1> is \
normally the current image (the one the user is changing); later pictures are images the user \
attached to this message.
- `current_image_prompt`: the prompt that made the current image, when it came from this chat. \
Treat it as the description of <Picture 1>; it does not need to be repeated.
- `history`: earlier messages and the prompts written for them, oldest first, for context \
("again", "undo that", "back to the earlier hair").
- `size` and `aspect`: the output canvas.

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
- Never cite a picture that is not listed.

## What may be drawn

Each picture has a `kind`. These rules decide what you may write a prompt for:

{rules}

If the request breaks a rule, don't write a prompt: return allowed=false, an empty prompt, and \
in `refusal` one short sentence to the user naming the rule. Don't write a toned-down version \
of a refused request.

## Output

Return {{"allowed": true, "prompt": ..., "reply": ..., "refusal": ""}}: `prompt` is the prompt \
only, as plain text; `reply` is one or two short sentences to the user saying what you \
changed or how you read the request (no prompt text in it).\
"""


class ExpPromptOutput(BaseModel):
    allowed: bool = True
    prompt: str = ""
    reply: str = ""
    refusal: str = ""


def writer_instructions() -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    guide = WRITER_GUIDE.format(rules=CONTENT_RULES)
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
    return ExpPromptOutput(prompt=prompt, reply="Offline sample: your message, lightly wrapped.")


def _aspect(size: str) -> str:
    return next((k for k, v in SIZES.items() if v == size), size)


# ---------------------------------------------------------------------------
# Runner


class ExperimentRunner:
    """Runs turns in the API process, one render at a time (the GPU makes one image at a
    time anyway). Unfinished turns don't survive a restart: `recover` marks them failed."""

    def __init__(
        self, repo: Any, media: MediaStore, images: ImageClient, writer_client: Any
    ) -> None:
        self.repo = repo
        self.media = media
        self.images = images
        self.writer_client = writer_client
        self._gpu = asyncio.Lock()
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

    async def run(self, turn_id: str) -> None:
        turn = self.repo.get_exp_turn(turn_id)
        if turn is None or turn.status != "pending":
            return
        turn.status = "running"
        self.repo.save_exp_turn(turn)
        try:
            # Read in a thread: up to nine images of several MB each.
            refs = await asyncio.to_thread(self._references, turn)
            if not turn.prompt:
                await self._write_prompt(turn)
                self.repo.save_exp_turn(turn)
            await self._guard(turn)
            for n in range(len(turn.results), turn.draws):
                seed = random.randint(1, 2**31 - 1)
                async with self._gpu:
                    result = await self.images.generate(turn.prompt, turn.size, seed, refs)
                ext = "png" if result.content_type == "image/png" else "jpg"
                rel = f"experiments/{turn.session_id}/{turn.turn_id}_{n}.{ext}"
                await asyncio.to_thread(self._write, rel, result.data)
                from .images import make_thumb

                thumb = await asyncio.to_thread(make_thumb, self.media, rel)
                turn.results.append(
                    ExpResult(file=rel, thumb=thumb, content_type=result.content_type, seed=seed)
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
            session.current = f"turn:{turn.turn_id}:0"
            session.updated_at = utcnow()
            self.repo.save_exp_session(session)

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
            if kind == "turn":
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
            parts.append(
                {"picture": f"<Picture {i}>", "what": what, "kind": picture_kind(ref, turns)}
            )
            path, mime = self.ref_file(ref, variant="thumb")  # the model only needs to see it
            if path is not None:
                media.append(MediaPart(label=f"<Picture {i}>", path=path, mime=mime))
        return parts, media, current_prompt, turns

    async def _guard(self, turn: ExpTurn) -> None:
        """Raise GuardRefused unless the turn's prompt may be drawn. A prompt the writer
        wrote was judged by the writer (it sees the rules and the pictures); one sent as
        typed (exact mode, or the writer failed) is screened by the text model."""
        if reason := text_guard(turn.prompt) or text_guard(turn.text):
            raise GuardRefused(reason)
        if turn.prompt_model is None:
            parts, media, _, _ = self._for_model(turn)
            await screen(self.writer_client, turn.prompt, pictures=parts, media=media)

    async def _write_prompt(self, turn: ExpTurn) -> None:
        if reason := text_guard(turn.text):  # clear cases need no model call
            raise GuardRefused(reason)
        parts, media, current_prompt, turns = self._for_model(turn)
        history = [
            {"message": t.text, "prompt": t.prompt}
            for t in turns
            if t.mode != "upload" and t.status == "done"
        ][-HISTORY_TURNS:]
        request = GenerationRequest(
            stage=STAGE,  # type: ignore[arg-type]
            instructions=writer_instructions(),
            context={
                "message": turn.text,
                "pictures": parts,
                "current_image_prompt": current_prompt,
                "history": history,
                "size": turn.size,
                "aspect": _aspect(turn.size),
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
            self._send_as_written(turn, ValueError(problem))
            return
        turn.prompt = prompt
        turn.reply = out.reply.strip()
        turn.prompt_model = request.model

    def _send_as_written(self, turn: ExpTurn, exc: Exception) -> None:
        """Fallback when the writer fails: the message is the prompt, with the same checks
        as an exact prompt (prompt_model stays None, so _guard screens it)."""
        if missing := cited_beyond(turn.text, len(turn.references)):
            raise ExpError(
                f"Couldn't rewrite your message ({exc}), and as written it cites "
                f"{', '.join(missing)}, which this message doesn't send."
            ) from exc
        turn.prompt = turn.text
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
