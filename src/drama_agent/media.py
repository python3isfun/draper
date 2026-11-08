"""Uploaded images and videos: storage, validation and what the model gets to see.

- Images are kept as uploaded, plus a downscaled JPEG for the model.
- Videos get evenly spaced keyframes (JPEG) at upload time, and an audio transcript at job
  time (it needs the model gateway), cached on the asset so retries don't pay again.

The type of a file is sniffed from its bytes; the client's Content-Type and filename are
never trusted, and the filename is never used as a path. ffmpeg/ffprobe run without a
shell, with a timeout. Video needs ffmpeg; without it, video uploads are refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import JobRequest, MediaKind, SourceAsset, VideoFrame

log = logging.getLogger(__name__)

MB = 1024 * 1024

# Sniffed type -> (kind, extension).
_TYPES: dict[str, tuple[MediaKind, str]] = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/webp": ("image", "webp"),
    "image/gif": ("image", "gif"),
    "video/mp4": ("video", "mp4"),
    "video/quicktime": ("video", "mov"),
    "video/webm": ("video", "webm"),
}


class MediaError(ValueError):
    """The upload is not acceptable (type, size, unreadable). Safe to show the user."""


class UploadTooLarge(MediaError):
    pass


def sniff(head: bytes) -> str | None:
    """Content type from the first bytes of a file, or None if unsupported."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in _MP4_BRANDS:
            return "video/mp4"
        return None  # HEIC/AVIF photos, M4A audio, etc. share the container
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"  # Matroska/WebM; ffprobe decides whether it's readable
    return None


# Major brands of ISO-BMFF files that are video (MP4 family, incl. 3GP and fragmented MP4).
_MP4_BRANDS = {
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1",
    b"M4V ", b"M4VH", b"M4VP", b"dash", b"mmp4", b"MSNV", b"3gp4", b"3gp5", b"3gp6",
    b"3g2a", b"f4v ",
}  # fmt: skip
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}


def _sniff_or_raise(head: bytes) -> str:
    content_type = sniff(head)
    if content_type is None:
        if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
            raise MediaError(
                "HEIC/AVIF photos aren't supported yet; export the photo as JPEG or PNG"
            )
        raise MediaError(
            "unsupported file type; upload a JPEG, PNG, WebP or GIF image, "
            "or an MP4, MOV or WebM video"
        )
    return content_type


@dataclass
class MediaLimits:
    max_image_bytes: int = 20 * MB
    max_video_bytes: int = 500 * MB
    max_video_s: float = 3600.0
    video_frames: int = 24  # keyframes sampled per video
    frame_px: int = 768  # longest side of a keyframe
    image_px: int = 1536  # longest side of the image copy sent to the model
    transcribe_max_s: float = 1800.0  # audio after this is not transcribed
    # Images in one model request (uploaded images + video keyframes). Videos share what
    # the images leave, so each video's keyframes are thinned evenly to fit.
    max_images_per_request: int = 48
    max_media_sources: int = 12  # image/video sources per job
    # Without ffmpeg images are sent as uploaded, so they must already be small.
    max_unscaled_image_bytes: int = 5 * MB


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, timeout=timeout, check=True, stdin=None)


def _scale(px: int) -> str:
    # Fit within px x px, never upscale, keep even dimensions for JPEG encoders.
    return (
        f"scale='if(gt(iw,ih),min({px},iw),-2)':'if(gt(iw,ih),-2,min({px},ih))',"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


class MediaStore:
    def __init__(self, root: str | Path, limits: MediaLimits | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or MediaLimits()

    def path(self, rel: str) -> Path:
        """Absolute path of a stored file; refuses anything outside the store."""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise MediaError(f"path outside the media store: {rel}")
        return p

    # ------------------------------------------------------------------
    # Upload

    async def save_upload(
        self,
        chunks: AsyncIterator[bytes],
        filename: str,
        declared_size: int | None = None,
    ) -> SourceAsset:
        """Stream an upload to disk, then validate and process it. Raises MediaError."""
        asset_id = f"asset_{uuid.uuid4().hex[:12]}"
        folder = self.root / asset_id
        folder.mkdir()
        tmp = folder / "upload.part"
        try:
            content_type, size, digest = await self._receive(chunks, tmp, declared_size)
            kind, ext = _TYPES[content_type]
            original = folder / f"original.{ext}"
            tmp.rename(original)
            asset = SourceAsset(
                asset_id=asset_id,
                kind=kind,
                filename=Path(filename or "upload").name[:200],
                content_type=content_type,
                size_bytes=size,
                sha256=digest,
                file=f"{asset_id}/{original.name}",
            )
            await asyncio.to_thread(self._process, asset)
            return asset
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    async def _receive(
        self, chunks: AsyncIterator[bytes], dest: Path, declared: int | None
    ) -> tuple[str, int, str]:
        lim = self.limits
        biggest = max(lim.max_image_bytes, lim.max_video_bytes)
        if declared is not None and declared > biggest:
            raise UploadTooLarge(f"file is larger than {biggest // MB} MB")
        sha = hashlib.sha256()
        size = 0
        head = b""
        content_type: str | None = None
        limit = biggest
        buf = bytearray()
        f = await asyncio.to_thread(dest.open, "wb")
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                if content_type is None and len(head) < 12:
                    head += chunk[: 12 - len(head)]
                    if len(head) == 12:
                        content_type = _sniff_or_raise(head)
                        kind = _TYPES[content_type][0]
                        limit = lim.max_image_bytes if kind == "image" else lim.max_video_bytes
                        if declared is not None and declared > limit:
                            raise UploadTooLarge(f"{kind} is larger than {limit // MB} MB")
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(f"file is larger than {limit // MB} MB")
                sha.update(chunk)
                buf += chunk
                if len(buf) >= MB:  # disk writes run in a thread, not on the event loop
                    await asyncio.to_thread(f.write, bytes(buf))
                    buf.clear()
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
        finally:
            await asyncio.to_thread(f.close)
        if size == 0:
            raise MediaError("empty upload")
        return content_type or _sniff_or_raise(head), size, sha.hexdigest()

    # ------------------------------------------------------------------
    # Processing (blocking; runs in a thread)

    def _process(self, asset: SourceAsset) -> None:
        if asset.kind == "image":
            self._process_image(asset)
        else:
            self._process_video(asset)

    def _process_image(self, asset: SourceAsset) -> None:
        if not ffmpeg_available():
            cap = self.limits.max_unscaled_image_bytes
            if asset.size_bytes > cap:
                raise UploadTooLarge(
                    f"image is larger than {cap // MB} MB; the server can't downscale it "
                    "(ffmpeg is not installed)"
                )
            asset.model_image = asset.file  # sent as uploaded
            return
        src = self.path(asset.file)
        out = src.with_name("model.jpg")
        try:
            info = self._probe(src)
            stream = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
            asset.width, asset.height = stream.get("width"), stream.get("height")
            _run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
                 "-frames:v", "1", "-vf", _scale(self.limits.image_px), "-q:v", "3", str(out)],
                timeout=60,
            )  # fmt: skip
            asset.model_image = f"{asset.asset_id}/model.jpg"
        except (subprocess.SubprocessError, StopIteration, OSError) as exc:
            raise MediaError("the image could not be read") from exc

    def _process_video(self, asset: SourceAsset) -> None:
        if not ffmpeg_available():
            raise MediaError("video uploads need ffmpeg on the server")
        src = self.path(asset.file)
        try:
            info = self._probe(src)
        except (subprocess.SubprocessError, OSError) as exc:
            raise MediaError("the video could not be read") from exc
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise MediaError("the file has no video stream")
        duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
        if duration <= 0:
            raise MediaError("could not determine the video's duration")
        if duration > self.limits.max_video_s:
            raise MediaError(f"video is longer than {self.limits.max_video_s / 60:.0f} minutes")
        asset.width, asset.height = video.get("width"), video.get("height")
        asset.duration_ms = round(duration * 1000)
        asset.has_audio = any(s.get("codec_type") == "audio" for s in streams)
        asset.transcript_status = "pending" if asset.has_audio else "no_audio"
        asset.frames = self._extract_frames(asset, src, duration)
        if not asset.frames:
            raise MediaError("no frames could be extracted from the video")

    def _extract_frames(self, asset: SourceAsset, src: Path, duration: float) -> list[VideoFrame]:
        # One frame from the middle of each of n equal slices: covers the whole video
        # evenly, and short clips get at most one frame per second.
        n = max(1, min(self.limits.video_frames, math.ceil(duration)))

        def grab(i: int) -> VideoFrame | None:
            t = (i + 0.5) * duration / n
            rel = f"{asset.asset_id}/frame_{i:03d}.jpg"
            try:
                _run(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{t:.3f}",
                     "-i", str(src), "-frames:v", "1", "-vf", _scale(self.limits.frame_px),
                     "-q:v", "4", str(self.path(rel))],
                    timeout=60,
                )  # fmt: skip
            except subprocess.SubprocessError:
                log.warning("asset %s: no frame at %.1fs", asset.asset_id, t)
                return None
            return VideoFrame(t_ms=round(t * 1000), file=rel) if self.path(rel).exists() else None

        # Seeking makes each grab cheap; a few in parallel keep long uploads responsive.
        with ThreadPoolExecutor(max_workers=4) as pool:
            return [f for f in pool.map(grab, range(n)) if f is not None]

    @staticmethod
    def _probe(src: Path) -> dict[str, Any]:
        out = _run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             str(src)],
            timeout=30,
        )  # fmt: skip
        return json.loads(out.stdout or b"{}")

    def extract_audio(self, asset: SourceAsset) -> tuple[Path, int | None]:
        """Mono 16 kHz MP3 for transcription. Returns (path, cut_ms): cut_ms is where the
        audio was cut off by the length cap, or None if it's complete. The file name is
        unique per call, so concurrent extractions never write the same file; the caller
        deletes it."""
        out = self.path(f"{asset.asset_id}/audio-{uuid.uuid4().hex[:8]}.mp3")
        cap = self.limits.transcribe_max_s
        cut = None
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(self.path(asset.file))]
        if asset.duration_ms and asset.duration_ms > cap * 1000:
            args += ["-t", str(cap)]
            cut = round(cap * 1000)
        _run([*args, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out)], timeout=600)
        return out, cut

    def delete(self, asset_id: str) -> None:
        shutil.rmtree(self.path(asset_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# What the ingest step sees


@dataclass
class MediaPart:
    """One image handed to the model, with the text label placed right before it."""

    label: str
    path: Path
    mime: str = "image/jpeg"


def _clock(ms: int) -> str:
    s = ms / 1000
    return f"{int(s // 60):02d}:{s % 60:04.1f}"


def source_media(
    request: JobRequest, assets: dict[str, SourceAsset], store: MediaStore
) -> tuple[list[dict[str, Any]], list[MediaPart]]:
    """Context entries (JSON) and images for every image/video source of a job."""
    context: list[dict[str, Any]] = []
    parts: list[MediaPart] = []
    media = [assets.get(s.asset_id or "") for s in request.sources if s.type in ("image", "video")]
    n_images = sum(1 for a in media if a and a.kind == "image")
    n_videos = sum(1 for a in media if a and a.kind == "video")
    per_video = max(1, (store.limits.max_images_per_request - n_images) // max(n_videos, 1))
    for i, src in enumerate(request.sources):
        if src.type not in ("image", "video") or not src.asset_id:
            continue
        asset = assets.get(src.asset_id)
        if asset is None:
            context.append({"source_index": i, "asset_id": src.asset_id, "missing": True})
            continue
        entry: dict[str, Any] = {
            "source_index": i,
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "user_prompt": src.text or None,
        }
        if asset.kind == "image":
            rel = asset.model_image or asset.file
            mime = "image/jpeg" if asset.model_image else asset.content_type
            entry["attached_images"] = 1
            parts.append(MediaPart(f"source[{i}] image ({asset.asset_id})", store.path(rel), mime))
        else:
            duration = asset.duration_ms or 0
            frames = _spread(asset.frames, per_video)
            entry |= {
                "duration_ms": duration,
                "keyframes_ms": [f.t_ms for f in frames],
                "audio": _audio_summary(asset),
            }
            if asset.transcript_status == "done":
                entry["transcript"] = [s.model_dump() for s in asset.transcript]
            for f in frames:
                parts.append(
                    MediaPart(
                        f"source[{i}] video ({asset.asset_id}) frame at {_clock(f.t_ms)} "
                        f"(t={f.t_ms} ms of {duration} ms)",
                        store.path(f.file),
                    )
                )
        context.append(entry)
    return context, parts


def _spread(items: list[VideoFrame], k: int) -> list[VideoFrame]:
    """k items spread evenly over the list (all of them if it's short enough)."""
    if len(items) <= k:
        return items
    return [items[int((j + 0.5) * len(items) / k)] for j in range(k)]


def _audio_summary(asset: SourceAsset) -> str:
    match asset.transcript_status:
        case "no_audio":
            return "the video has no audio track"
        case "done":
            if asset.transcript_until_ms is not None:
                return (
                    f"transcribed up to {asset.transcript_until_ms} ms; audio after that was "
                    "not analyzed (unknown)"
                )
            return "speech transcribed in full (non-speech sound was not analyzed)"
        case "failed":
            return f"transcription failed ({asset.transcript_error}); audio not analyzed (unknown)"
        case _:
            return "audio was not analyzed (no transcription available); treat it as unknown"


def media_from_env(default_dir: str = "data/media") -> MediaStore:
    def mb(name: str, default: str) -> int:
        return int(float(os.environ.get(name, default)) * MB)

    limits = MediaLimits(
        max_image_bytes=mb("DRAMA_MAX_IMAGE_MB", "20"),
        max_video_bytes=mb("DRAMA_MAX_VIDEO_MB", "500"),
        max_video_s=float(os.environ.get("DRAMA_MAX_VIDEO_S", "3600")),
        video_frames=int(os.environ.get("DRAMA_VIDEO_FRAMES", "24")),
        transcribe_max_s=float(os.environ.get("DRAMA_TRANSCRIBE_MAX_S", "1800")),
        max_images_per_request=int(os.environ.get("DRAMA_MAX_IMAGES_PER_REQUEST", "48")),
    )
    return MediaStore(os.environ.get("DRAMA_MEDIA_DIR", default_dir), limits)
