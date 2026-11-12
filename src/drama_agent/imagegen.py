"""Image generation clients.

- MiniMaxImageClient: MiniMax-H3-Image on MCPHub's GPU route
  (https://api.mcphub.com/api/v3/gpu-images). Text to image, or editing with up to nine
  reference images, which the prompt calls <Picture 1>, <Picture 2>, ... in order. The
  server works on one image at a time and queues the rest, so callers send one request at
  a time. Auth is the MCPHub key in X-API-Key (Bearer is refused on this route).
- FixtureImageClient: draws a small solid-color PNG locally, for tests and offline mode.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Protocol

import httpx

RETRYABLE_STATUS = {429, 502, 503, 504}
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
        max_retries: int = 2,
        backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MCPHub API key is empty")
        self.model_id = model
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
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise ImageGenError(f"image generation failed: {last}")


class FixtureImageClient:
    """Offline stand-in: a small PNG whose color derives from the prompt and seed."""

    model_id = "fixture-image-v1"

    def __init__(self, delay_s: float = 0.0) -> None:
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
