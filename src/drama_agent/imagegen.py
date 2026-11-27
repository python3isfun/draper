"""Image generation clients.

- MiniMaxImageClient: MiniMax-H3-Image on MCPHub's GPU route
  (https://api.mcphub.com/api/v3/gpu-images). Text to image, or editing with up to nine
  reference images, which the prompt calls <Picture 1>, <Picture 2>, ... in order. The
  server works on one image at a time and queues the rest, so callers send one request at
  a time. Auth is the MCPHub key in X-API-Key (Bearer is refused on this route).
- GeminiImageClient: Google's Gemini image models (Nano Banana 2.1 by default) over the
  Gemini API's generateContent, or a gateway serving the same API. Up to eight reference
  images, each sent as a part introduced by "Picture N:"; the prompt's <Picture N> labels
  become "Picture N". The same request shape as dramer's gemini adapter.
- LocalImageClient: a simple image server of your own (POST /image with a `prompt` form
  field, answering with the image), text to image only. Face Lab tests can use it.
- FixtureImageClient: draws a small solid-color PNG locally, for tests and offline mode.

Every client has a `model_id` and `max_references` (the most reference images it takes).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Protocol

import httpx

RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_BACKOFF_S = 120.0  # the longest wait between two tries of the MCPHub route
MAX_REFERENCES = 9
MAX_REFERENCE_BYTES = 24 * 1024 * 1024


class ImageGenError(RuntimeError):
    """Image generation failed. Messages never include the API key."""


@dataclass
class Reference:
    name: str
    data: bytes
    mime: str = "image/jpeg"


@dataclass
class ImageResult:
    data: bytes
    content_type: str


class ImageClient(Protocol):
    model_id: str
    max_references: int

    async def generate(
        self, prompt: str, size: str, seed: int, references: list[Reference]
    ) -> ImageResult: ...


class MiniMaxImageClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mcphub.com/api/v3/gpu-images",
        model: str = "minimax-h3-image",
        *,
        timeout_s: float = 600.0,  # the GPU queues requests; MCPHub itself gives up at 300 s
        # The model server answers 502 at once whenever it is busy or restarting, for
        # five minutes at a time: retries 5, 10, 20, 40, 80 s apart, then every 2 minutes,
        # ride that out (eight retries span about nine minutes).
        max_retries: int = 8,
        backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MCPHub API key is empty")
        self.model_id = model
        self.max_references = MAX_REFERENCES
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/{model}",
            headers={"X-API-Key": api_key},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def generate(
        self, prompt: str, size: str, seed: int, references: list[Reference]
    ) -> ImageResult:
        if len(references) > MAX_REFERENCES:
            raise ImageGenError(f"at most {MAX_REFERENCES} reference images")
        if sum(len(r.data) for r in references) > MAX_REFERENCE_BYTES:
            raise ImageGenError("reference images exceed 24 MiB in total")
        fields = {"prompt": prompt, "size": size, "seed": seed, "output_format": "jpeg"}
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                if references:
                    resp = await self._http.post(
                        "/edits",
                        data={k: str(v) for k, v in fields.items()},
                        files=[("image", (r.name, r.data, r.mime)) for r in references],
                    )
                else:
                    resp = await self._http.post("/generations", json=fields)
            except httpx.TimeoutException as exc:
                # Not retried: the GPU server may still be rendering this request, and a
                # resend would queue the same image again behind it.
                raise ImageGenError(f"image request timed out ({exc!r})") from exc
            except httpx.TransportError as exc:
                last = f"transport error: {exc!r}"
            else:
                if resp.status_code == 200:
                    try:
                        b64 = resp.json()["data"][0]["b64_json"]
                        return ImageResult(base64.b64decode(b64), "image/jpeg")
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise ImageGenError(f"unexpected response: {exc!r}") from exc
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ImageGenError(f"image generation failed: {last}")
            if attempt < self.max_retries:
                await asyncio.sleep(min(self.backoff_s * 2**attempt, MAX_BACKOFF_S))
        raise ImageGenError(f"image generation failed: {last}")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-nano-banana-2.1"
GEMINI_MAX_REFERENCES = 8
GEMINI_MAX_INLINE_BYTES = 20 * 1024 * 1024  # base64 included
# imageConfig.aspectRatio values the models take.
GEMINI_ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
# Finish reasons that are the model declining, not a fault here.
GEMINI_REFUSED = {"SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "IMAGE_PROHIBITED_CONTENT",
                  "BLOCKLIST", "SPII", "RECITATION", "IMAGE_RECITATION"}  # fmt: skip
PICTURE = re.compile(r"<\s*Picture\s+(\d+)\s*>", re.IGNORECASE)


def gemini_aspect(size: str) -> str:
    """The aspect ratio the Gemini models take that is nearest to a "WxH" size."""
    w, h = (int(x) for x in size.lower().split("x"))

    def ratio(a: str) -> float:
        x, y = a.split(":")
        return int(x) / int(y)

    return min(GEMINI_ASPECTS, key=lambda a: abs(ratio(a) - w / h))


def gemini_size(size: str) -> str:
    """imageConfig.imageSize for a "WxH" size: the tier whose long edge covers it (1K is
    about 1024 px on the long edge, 2K about 2048-2752, 4K above)."""
    long_edge = max(int(x) for x in size.lower().split("x"))
    return "1K" if long_edge <= 1408 else "2K" if long_edge <= 2816 else "4K"


class GeminiImageError(ImageGenError):
    """Gemini answered without an image; `refused` when the model declined it."""

    def __init__(self, message: str, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


class GeminiImageClient:
    """Nano Banana (Gemini image models) over generateContent; one image per request."""

    def __init__(
        self,
        api_key: str,
        base_url: str = GEMINI_URL,
        model: str = GEMINI_MODEL,
        *,
        timeout_s: float = 300.0,
        max_retries: int = 2,
        backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is empty")
        self.model_id = model
        self.max_references = GEMINI_MAX_REFERENCES
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-goog-api-key": api_key},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def body(self, prompt: str, size: str, seed: int, references: list[Reference]) -> dict:
        """The request body (pure, so the exact request is testable)."""
        parts: list[dict] = []
        for i, r in enumerate(references, 1):
            parts.append({"text": f"Picture {i}:"})
            parts.append({"inlineData": {"mimeType": r.mime,
                                         "data": base64.b64encode(r.data).decode()}})  # fmt: skip
        parts.append({"text": PICTURE.sub(lambda m: f"Picture {m.group(1)}", prompt)})
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": gemini_aspect(size), "imageSize": gemini_size(size)},
                "seed": seed,
            },
        }

    async def generate(
        self, prompt: str, size: str, seed: int, references: list[Reference]
    ) -> ImageResult:
        if len(references) > self.max_references:
            raise ImageGenError(f"at most {self.max_references} reference images")
        body = self.body(prompt, size, seed, references)
        inline = sum(len(p["inlineData"]["data"]) for p in body["contents"][0]["parts"]
                     if "inlineData" in p)  # fmt: skip
        if inline > GEMINI_MAX_INLINE_BYTES:
            raise ImageGenError("reference images exceed 20 MB in one request")
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.post(f"/models/{self.model_id}:generateContent", json=body)
            except httpx.TimeoutException as exc:
                raise ImageGenError(f"image request timed out ({exc!r})") from exc
            except httpx.TransportError as exc:
                last = f"transport error: {exc!r}"
            else:
                if resp.status_code == 200:
                    return gemini_image(resp.json())
                last = f"HTTP {resp.status_code}: {_gemini_error(resp.text)}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ImageGenError(f"image generation failed: {last}")
            if attempt < self.max_retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise ImageGenError(f"image generation failed: {last}")


def gemini_image(res: dict) -> ImageResult:
    """The image of a generateContent response, or the reason there is none."""
    if block := (res.get("promptFeedback") or {}).get("blockReason"):
        raise GeminiImageError(f"the request was blocked: {block}", refused=True)
    candidate = (res.get("candidates") or [{}])[0] or {}
    parts = (candidate.get("content") or {}).get("parts") or []
    for p in parts:
        data = (p.get("inlineData") or {}).get("data")
        if data:
            return ImageResult(base64.b64decode(data), p["inlineData"].get("mimeType", "image/png"))
    reason = candidate.get("finishReason")
    said = " ".join(p.get("text", "") for p in parts).strip()[:300]
    detail = f" ({reason})" if reason else ""
    detail += f": {said}" if said else ""
    if reason in GEMINI_REFUSED:
        raise GeminiImageError(f"the model declined this image{detail}", refused=True)
    raise GeminiImageError(f"the model returned no image{detail}")


def _gemini_error(text: str) -> str:
    """The provider's own words out of an error body ({error: {status, message}})."""
    try:
        err = (httpx.Response(200, content=text).json() or {}).get("error") or {}
    except ValueError:
        return text[:300]
    return f"{err.get('status', '')} {err.get('message', '')}".strip()[:300] or text[:300]


LOCAL_IMAGE_URL = "http://localhost:8080"


class LocalImageClient:
    """A simple image server of your own, text to image only:

        curl -s localhost:8080/image -F "prompt=a red fox in fresh snow" -o fox.png

    It is sent `prompt`, `size` ("WIDTHxHEIGHT") and `seed` as form fields (a server may
    ignore the last two) and answers with the image's bytes (PNG or JPEG)."""

    max_references = 0

    def __init__(
        self,
        base_url: str = LOCAL_IMAGE_URL,
        *,
        timeout_s: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = f"local ({self.base_url})"
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def generate(
        self, prompt: str, size: str, seed: int, references: list[Reference]
    ) -> ImageResult:
        if references:
            raise ImageGenError("the local image server draws from text only")
        fields = {"prompt": prompt, "size": size, "seed": str(seed)}
        try:
            # Multipart form fields, as curl -F sends them.
            resp = await self._http.post(
                f"{self.base_url}/image", files={k: (None, v) for k, v in fields.items()}
            )
        except httpx.TimeoutException as exc:
            raise ImageGenError(f"the local image server timed out ({exc!r})") from exc
        except httpx.TransportError as exc:
            raise ImageGenError(
                f"can't reach the local image server at {self.base_url} ({exc!r})"
            ) from exc
        if resp.status_code != 200:
            raise ImageGenError(
                f"the local image server failed: HTTP {resp.status_code}: {resp.text[:300]}"
            )
        mime = image_type(resp.content)
        if mime is None:
            said = resp.headers.get("content-type", "nothing")
            raise ImageGenError(f"the local image server sent {said}, not a PNG or JPEG image")
        return ImageResult(resp.content, mime)


def image_type(data: bytes) -> str | None:
    """PNG or JPEG by the file's first bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


class FixtureImageClient:
    """Offline stand-in: a small PNG whose color derives from the prompt and seed."""

    model_id = "fixture-image-v1"
    max_references = MAX_REFERENCES

    def __init__(
        self,
        delay_s: float = 0.0,
        model_id: str | None = None,
        max_references: int | None = None,
    ) -> None:
        if model_id:
            self.model_id = model_id
        if max_references is not None:
            self.max_references = max_references
        self.delay_s = delay_s
        self.calls: list[tuple[str, str, int, list[str]]] = []

    async def generate(
        self, prompt: str, size: str, seed: int, references: list[Reference]
    ) -> ImageResult:
        self.calls.append((prompt, size, seed, [r.name for r in references]))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        w, h = (int(x) for x in size.split("x"))
        scale = max(w, h) / 256  # keep the placeholder small
        digest = hashlib.sha256(f"{prompt}|{seed}".encode()).digest()
        return ImageResult(
            solid_png(max(1, round(w / scale)), max(1, round(h / scale)), digest[:3]), "image/png"
        )


def solid_png(width: int, height: int, rgb: bytes) -> bytes:
    """A minimal valid PNG of one color (no image library needed)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + rgb * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
