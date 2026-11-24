"""Music for an episode's scenes: ACE-Step on MCPHub's GPU music route, or an offline
fixture tone.

The route (`POST /api/v3/gpu-music/acestep/jobs`, then `GET /api/v3/gpu-music/jobs/{id}`)
takes a caption, a length in seconds, an optional tempo and key, and a list of seeds; every
seed is one take, rendered in one batched pass, and the same request gives the same takes.
A job is `queued` until its takes are saved, then `completed` with `audio_urls` (1 h links,
in seed order) and `takes` [{seed, url, size_bytes}], or `failed` / `lost`.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

MAX_MUSIC_S = 240
MIN_MUSIC_S = 10


class MusicGenError(RuntimeError):
    pass


@dataclass
class MusicTake:
    data: bytes  # the audio file (WAV from ACE-Step; the fixture makes WAV too)
    seed: int
    content_type: str = "audio/wav"
    job_id: str | None = None


@dataclass
class MusicRequest:
    caption: str
    duration_s: int
    bpm: int | None = None
    key: str | None = None
    seeds: list[int] = field(default_factory=lambda: [1])


class MusicClient(Protocol):
    model_id: str

    async def generate(self, request: MusicRequest) -> list[MusicTake]:
        """One take per seed, in seed order."""
        ...


def clamp_music_seconds(seconds: float) -> int:
    return max(MIN_MUSIC_S, min(MAX_MUSIC_S, math.ceil(seconds)))


class ACEStepClient:
    """ACE-Step through MCPHub's gpu-music route (one job at a time on the GPU; about 20 s
    for a 60 s two-seed job)."""

    model_id = "acestep"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mcphub.com",
        *,
        poll_s: float = 5.0,
        timeout_s: float = 1200.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.poll_s, self.timeout_s = poll_s, timeout_s
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"X-API-Key": api_key}, timeout=120.0, transport=transport
        )

    def body(self, request: MusicRequest) -> dict:
        body: dict = {
            "caption": request.caption.strip(),
            "duration_seconds": clamp_music_seconds(request.duration_s),
            "seeds": list(request.seeds) or [1],
        }
        if request.bpm:
            body["bpm"] = int(request.bpm)
        if request.key:
            body["key"] = request.key
        return body

    async def generate(self, request: MusicRequest) -> list[MusicTake]:
        if not request.caption.strip():
            raise MusicGenError("music needs a caption")
        resp = await self._http.post("/api/v3/gpu-music/acestep/jobs", json=self.body(request))
        if resp.status_code not in (200, 201, 202):
            raise MusicGenError(f"music job refused ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise MusicGenError(f"no job id in {str(data)[:200]}")
        deadline = asyncio.get_event_loop().time() + self.timeout_s
        while True:
            await asyncio.sleep(self.poll_s)
            r = await self._http.get(f"/api/v3/gpu-music/jobs/{job_id}")
            if r.status_code != 200:
                raise MusicGenError(f"music job {job_id}: {r.status_code} {r.text[:200]}")
            job = r.json()
            status = job.get("status")
            if status == "completed":
                break
            if status in ("failed", "lost", "cancelled"):
                raise MusicGenError(
                    f"music job {job_id} {status}: {job.get('error') or job.get('last_message')}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise MusicGenError(f"music job {job_id} took over {self.timeout_s:.0f} s")
        takes = job.get("takes") or [
            {"seed": s, "url": u}
            for s, u in zip(request.seeds, job.get("audio_urls") or [], strict=False)
        ]
        out = []
        for t in takes:
            r = await self._http.get(t["url"], follow_redirects=True)
            if r.status_code != 200:
                raise MusicGenError(f"music take download failed ({r.status_code})")
            out.append(MusicTake(r.content, seed=int(t.get("seed", 0)),
                                 content_type=r.headers.get("content-type", "audio/wav").split(";")[0],
                                 job_id=job_id))  # fmt: skip
        if not out:
            raise MusicGenError(f"music job {job_id} completed with no takes")
        return out


class FixtureMusicClient:
    """Offline stand-in: a soft two-note tone of the asked length (ffmpeg), the same for the
    same seed."""

    model_id = "fixture-music"

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: list[MusicRequest] = []

    async def generate(self, request: MusicRequest) -> list[MusicTake]:
        self.calls.append(request)
        if not request.caption.strip():
            raise MusicGenError("music needs a caption")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        secs = clamp_music_seconds(request.duration_s)
        return [MusicTake(await asyncio.to_thread(fixture_tone, secs, seed), seed=seed)
                for seed in (request.seeds or [1])]  # fmt: skip


def fixture_tone(seconds: int, seed: int) -> bytes:
    """A quiet two-note WAV `seconds` long; the notes depend on the seed."""
    base = 196 + (seed * 37) % 160
    expr = f"sin(2*PI*{base}*t)*0.15 + sin(2*PI*{base * 1.5:.1f}*t)*0.08"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tone.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"aevalsrc={expr}:s=48000:d={seconds}", "-ac", "2", str(out)],
                check=True, capture_output=True, timeout=120,
            )  # fmt: skip
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise MusicGenError("ffmpeg is needed to make fixture music") from exc
        return out.read_bytes()


def music_client_from_env() -> MusicClient:
    """DRAMA_MUSIC_CLIENT selects the backend: 'fixture' (default: an ffmpeg tone) or
    'mcphub' (ACE-Step on MCPHub's gpu-music route: MCPHUB_KEY; MCPHUB_MUSIC_URL, default
    https://api.mcphub.com). Real music is opt-in, like the text, image and video clients."""
    kind = os.environ.get("DRAMA_MUSIC_CLIENT", "fixture").lower()
    if kind == "fixture":
        return FixtureMusicClient()
    if kind == "mcphub":
        key = os.environ.get("MCPHUB_KEY")
        if not key:
            raise RuntimeError("DRAMA_MUSIC_CLIENT=mcphub but MCPHUB_KEY is not set")
        return ACEStepClient(key, base_url=os.environ.get("MCPHUB_MUSIC_URL", "https://api.mcphub.com"),
                             poll_s=float(os.environ.get("DRAMA_MUSIC_POLL_S", "5")))  # fmt: skip
    raise RuntimeError(f"unknown DRAMA_MUSIC_CLIENT {kind!r} (fixture or mcphub)")
