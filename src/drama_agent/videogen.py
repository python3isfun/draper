"""Video generation: a shot's clip from its H3 video prompt and its reference pictures.

The clip is rendered by MiniMax H3 in reference mode (Ref2VA): the approved keyframes and
the design pictures go with the prompt as `<Picture N>` references the cut matches, never
as start frames (that is a different H3 variant). `MiniMaxVideoClient` speaks MiniMax's own
video API (`POST /v2/video_generation`, then polling `GET /v2/query/video_generation/{id}`):
a task is created, waited for, and its file downloaded. `FixtureVideoClient` makes a plain
coloured clip of the right length with ffmpeg, so the episode flow runs offline and in
tests. `MCPHubVideoClient` uses the same model on MCPHub's GPU route
(`POST /api/v3/gpu-videos/minimax-h3-ref2va/jobs`, then `GET /api/v3/gpu-videos/jobs/{id}`),
which runs on the company's own H100s and takes the MCPHub key. All are chosen by
`video_client_from_env`: real renders are opt-in (`DRAMA_VIDEO_CLIENT=mcphub|minimax`), a
key in the environment alone never triggers GPU calls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

MIN_CLIP_S = 4
MAX_CLIP_S = 15
# Reference pictures H3 takes with one clip.
MAX_VIDEO_REFERENCES = 9
MAX_PROMPT_CHARS = 7000
H3_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class VideoGenError(RuntimeError):
    """Video generation failed. Messages never include the API key."""


@dataclass
class VideoReference:
    name: str
    data: bytes
    mime: str = "image/jpeg"


@dataclass
class VideoResult:
    data: bytes
    content_type: str = "video/mp4"
    model: str = ""
    task_id: str | None = None
    notes: list[str] = field(default_factory=list)


class VideoClient(Protocol):
    model_id: str
    max_references: int

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        ratio: str,
        references: list[VideoReference],
        short_edge: int | None = None,
        seed: int | None = None,
    ) -> VideoResult: ...


def clamp_seconds(ms: int) -> int:
    """A clip's length in whole seconds, within H3's 4-15 s (rounded up: a line timed to
    the clip's end must still fit)."""
    return max(MIN_CLIP_S, min(MAX_CLIP_S, -(-ms // 1000)))


class MiniMaxVideoClient:
    """MiniMax's video API. `base_url` is the API host (the official one by default; a
    gateway that mirrors the API works too)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimax.io",
        model: str = "MiniMax-H3",
        resolution: str = "768P",
        *,
        poll_s: float = 10.0,
        timeout_s: float = 3600.0,  # a clip queues behind others; H3 takes minutes
        max_retries: int = 2,
        backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MiniMax API key is empty")
        self.model_id = model
        self.resolution = resolution
        self.max_references = MAX_VIDEO_REFERENCES
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(120.0, read=300.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def body(
        self,
        prompt: str,
        duration_s: int,
        ratio: str,
        references: list[VideoReference],
        short_edge: int | None = None,
        seed: int | None = None,
    ) -> dict:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for r in references:
            fmt = r.mime.split("/", 1)[-1].lower().replace("jpg", "jpeg")
            url = f"data:image/{fmt};base64,{base64.b64encode(r.data).decode()}"
            content.append({"type": "image_url", "image_url": {"url": url},
                            "role": "reference_image"})  # fmt: skip
        return {
            "model": self.model_id,
            "content": content,
            "duration": max(MIN_CLIP_S, min(MAX_CLIP_S, int(duration_s))),
            "resolution": f"{short_edge}P" if short_edge else self.resolution,
            "ratio": ratio if ratio in H3_RATIOS else "adaptive",
            # MiniMax's API has no documented seed: takes differ by the model's own
            # randomness there; `seed` is honored on the MCPHub route.
            "generate_audio": True,
        }

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        ratio: str,
        references: list[VideoReference],
        short_edge: int | None = None,
        seed: int | None = None,
    ) -> VideoResult:
        if len(references) > MAX_VIDEO_REFERENCES:
            raise VideoGenError(f"at most {MAX_VIDEO_REFERENCES} reference pictures")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise VideoGenError(f"the prompt is over {MAX_PROMPT_CHARS} characters")
        if not references:
            raise VideoGenError("a clip needs at least one reference picture")
        body = self.body(prompt, duration_s, ratio, references, short_edge, seed)
        task_id = await self._create(body)
        url = await self._wait(task_id)
        return VideoResult(await self._download(url), model=self.model_id, task_id=task_id)

    async def _create(self, body: dict) -> str:
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.post("/v2/video_generation", json=body)
            except httpx.TransportError as exc:
                last = f"transport error: {exc!r}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    task_id = data.get("task_id") or (data.get("task") or {}).get("task_id")
                    if not task_id:
                        raise VideoGenError(f"no task id in the response: {resp.text[:300]}")
                    return str(task_id)
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise VideoGenError(f"video generation refused: {last}")
            if attempt < self.max_retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise VideoGenError(f"video generation failed: {last}")

    async def _wait(self, task_id: str) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            try:
                resp = await self._http.get(f"/v2/query/video_generation/{task_id}")
            except httpx.TransportError as exc:
                log.info("video task %s: poll failed (%r), trying again", task_id, exc)
                resp = None
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                task = data.get("task") or data
                status = str(task.get("status", "")).lower()
                if status == "succeeded":
                    url = (task.get("content") or {}).get("url") or task.get("url")
                    if not url:
                        raise VideoGenError("the task succeeded but gave no file url")
                    return str(url)
                if status in ("failed", "cancelled"):
                    err = task.get("error") or data.get("error") or status
                    raise VideoGenError(f"video task {status}: {str(err)[:300]}")
            elif resp is not None and resp.status_code not in RETRYABLE_STATUS:
                raise VideoGenError(f"video task query failed: HTTP {resp.status_code}: "
                                    f"{resp.text[:300]}")  # fmt: skip
            if loop.time() > deadline:
                raise VideoGenError(f"video task {task_id} didn't finish in {self.timeout_s:.0f} s")
            await asyncio.sleep(self.poll_s)

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0)) as http:
            resp = await http.get(url)
        if resp.status_code != 200:
            raise VideoGenError(f"the clip couldn't be downloaded: HTTP {resp.status_code}")
        return resp.content


class MCPHubVideoClient(MiniMaxVideoClient):
    """MiniMax-H3 Ref2VA on MCPHub's GPU route. One clip renders at a time on the shared
    server (about 3.5 minutes for 4 s at 768p), so a job may wait behind others; the same
    request with the same seed gives the identical clip."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mcphub.com",
        model: str = "minimax-h3-ref2va",
        short_edge: int = 768,
        *,
        seed: int = 1,
        poll_s: float = 15.0,
        timeout_s: float = 3600.0,
        max_retries: int = 2,
        backoff_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(api_key, base_url, model, str(short_edge), poll_s=poll_s,
                         timeout_s=timeout_s, max_retries=max_retries, backoff_s=backoff_s,
                         transport=transport)  # fmt: skip
        self.short_edge = short_edge
        self.seed = seed
        self._http.headers.pop("Authorization", None)
        self._http.headers["X-API-Key"] = api_key

    def body(
        self,
        prompt: str,
        duration_s: int,
        ratio: str,
        references: list[VideoReference],
        short_edge: int | None = None,
        seed: int | None = None,
    ) -> dict:
        conditions = []
        for r in references:
            fmt = r.mime.split("/", 1)[-1].lower().replace("jpg", "jpeg")
            uri = f"data:image/{fmt};base64,{base64.b64encode(r.data).decode()}"
            conditions.append({"type": "image", "role": "reference", "uri": uri})
        return {
            "task": "ref2va",
            "seed": self.seed if seed is None else seed,
            "prompt": prompt,
            "target": {
                "short_edge": short_edge or self.short_edge,
                "aspect_ratio": ratio if ratio in H3_RATIOS else "auto",
                "duration_seconds": max(MIN_CLIP_S, min(MAX_CLIP_S, int(duration_s))),
            },
            "conditions": conditions,
        }

    async def _create(self, body: dict) -> str:
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.post(f"/api/v3/gpu-videos/{self.model_id}/jobs", json=body)
            except httpx.TransportError as exc:
                last = f"transport error: {exc!r}"
            else:
                if resp.status_code in (200, 201, 202):
                    data = resp.json()
                    job_id = data.get("id") or data.get("job_id")
                    if not job_id:
                        raise VideoGenError(f"no job id in the response: {resp.text[:300]}")
                    return str(job_id)
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise VideoGenError(f"video generation refused: {last}")
            if attempt < self.max_retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise VideoGenError(f"video generation failed: {last}")

    async def _wait(self, task_id: str) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            try:
                resp = await self._http.get(f"/api/v3/gpu-videos/jobs/{task_id}")
            except httpx.TransportError as exc:
                log.info("video job %s: poll failed (%r), trying again", task_id, exc)
                resp = None
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                status = str(data.get("status", "")).lower()
                if status in ("completed", "succeeded"):
                    url = data.get("video_url") or (data.get("output") or {}).get("video_url")
                    if not url:
                        raise VideoGenError("the job completed but gave no video url")
                    return str(url)
                if status in ("failed", "cancelled", "canceled", "error"):
                    err = data.get("error") or data.get("message") or status
                    raise VideoGenError(f"video job {status}: {str(err)[:300]}")
            elif resp is not None and resp.status_code not in RETRYABLE_STATUS:
                raise VideoGenError(f"video job query failed: HTTP {resp.status_code}: "
                                    f"{resp.text[:300]}")  # fmt: skip
            if loop.time() > deadline:
                raise VideoGenError(f"video job {task_id} didn't finish in {self.timeout_s:.0f} s")
            await asyncio.sleep(self.poll_s)


def ffmpeg_available() -> bool:
    from shutil import which

    return which("ffmpeg") is not None


def synthetic_clip(duration_s: int, ratio: str, color: str, label: str = "") -> bytes:
    """A plain coloured clip with silent audio, `duration_s` long (ffmpeg)."""
    w, h = {"16:9": (576, 320), "9:16": (320, 576), "1:1": (384, 384), "4:3": (448, 336),
            "3:4": (336, 448), "21:9": (672, 288)}.get(ratio, (320, 576))  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="drama-clip-") as tmp:
        out = Path(tmp) / "clip.mp4"
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={duration_s}:r=24",
            "-f", "lavfi", "-i", "anullsrc=r=32000:cl=stereo",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(out),
        ]  # fmt: skip
        try:
            subprocess.run(args, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            err = getattr(exc, "stderr", b"") or b""
            raise VideoGenError(f"ffmpeg couldn't make the clip: {err[-300:]!r}") from exc
        return out.read_bytes()


class FixtureVideoClient:
    """Offline clips: a coloured clip of the right length (the colour from the prompt, so
    different shots look different). Needs ffmpeg."""

    model_id = "fixture-video"
    max_references = MAX_VIDEO_REFERENCES

    def __init__(self, delay_s: float = 0.0, model_id: str = "fixture-video") -> None:
        self.delay_s = delay_s
        self.model_id = model_id
        self.calls: list[dict] = []

    async def generate(
        self,
        prompt: str,
        duration_s: int,
        ratio: str,
        references: list[VideoReference],
        short_edge: int | None = None,
        seed: int | None = None,
    ) -> VideoResult:
        self.calls.append({"prompt": prompt, "duration_s": duration_s, "ratio": ratio,
                           "references": [r.name for r in references],
                           "short_edge": short_edge, "seed": seed})  # fmt: skip
        if not references:
            raise VideoGenError("a clip needs at least one reference picture")
        if not ffmpeg_available():
            raise VideoGenError("ffmpeg is needed to make fixture clips")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        color = "#" + hashlib.sha1(f"{prompt}|{seed}".encode()).hexdigest()[:6]
        secs = max(MIN_CLIP_S, min(MAX_CLIP_S, int(duration_s)))
        data = await asyncio.to_thread(synthetic_clip, secs, ratio, color)
        return VideoResult(data, model=self.model_id)


def video_client_from_env() -> VideoClient:
    """DRAMA_VIDEO_CLIENT selects the backend: 'fixture' (default: ffmpeg-made coloured
    clips), 'mcphub' (MiniMax-H3 Ref2VA on MCPHub's GPU route: MCPHUB_KEY; MCPHUB_VIDEO_URL, default https://api.mcphub.com;
    MCPHUB_VIDEO_MODEL, default minimax-h3-ref2va; DRAMA_VIDEO_SHORT_EDGE, default 768;
    DRAMA_VIDEO_SEED) or 'minimax' (MINIMAX_API_KEY; MINIMAX_VIDEO_URL for a gateway, default
    https://api.minimax.io; MINIMAX_VIDEO_MODEL, default MiniMax-H3; DRAMA_VIDEO_RESOLUTION,
    default 768P). Real renders are opt-in, like the text and image clients."""
    kind = os.environ.get("DRAMA_VIDEO_CLIENT", "fixture").lower()
    if kind == "fixture":
        return FixtureVideoClient(delay_s=float(os.environ.get("DRAMA_FIXTURE_DELAY_S", "0")))
    if kind == "mcphub":
        key = os.environ.get("MCPHUB_KEY")
        if not key:
            raise RuntimeError("DRAMA_VIDEO_CLIENT=mcphub but MCPHUB_KEY is not set")
        return MCPHubVideoClient(
            api_key=key,
            base_url=os.environ.get("MCPHUB_VIDEO_URL", "https://api.mcphub.com"),
            model=os.environ.get("MCPHUB_VIDEO_MODEL", "minimax-h3-ref2va"),
            short_edge=int(os.environ.get("DRAMA_VIDEO_SHORT_EDGE", "768")),
            seed=int(os.environ.get("DRAMA_VIDEO_SEED", "1")),
            poll_s=float(os.environ.get("DRAMA_VIDEO_POLL_S", "15")),
        )
    if kind == "minimax":
        key = os.environ.get("MINIMAX_API_KEY")
        if not key:
            raise RuntimeError("DRAMA_VIDEO_CLIENT=minimax but MINIMAX_API_KEY is not set")
        return MiniMaxVideoClient(
            api_key=key,
            base_url=os.environ.get("MINIMAX_VIDEO_URL", "https://api.minimax.io"),
            model=os.environ.get("MINIMAX_VIDEO_MODEL", "MiniMax-H3"),
            resolution=os.environ.get("DRAMA_VIDEO_RESOLUTION", "768P"),
            poll_s=float(os.environ.get("DRAMA_VIDEO_POLL_S", "10")),
        )
    raise RuntimeError(f"unknown DRAMA_VIDEO_CLIENT={kind!r}")
