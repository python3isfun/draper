"""Assembling an episode: its shots' clips, one after another, into one file (ffmpeg).

The clips are concatenated in storyboard order with their own sound; each is scaled and
padded to the first clip's size and re-encoded, so clips of different sizes or codecs join
cleanly. No music, titles or transitions: the cut is the editor's; this is the rough
assembly that shows the episode as rendered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class AssembleError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 1800.0) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise AssembleError(f"ffmpeg failed: {exc.stderr[-400:].decode(errors='replace')}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AssembleError("ffmpeg took too long") from exc
    except FileNotFoundError as exc:
        raise AssembleError("ffmpeg is not installed") from exc


def probe(path: Path) -> dict[str, Any]:
    """Width, height, duration and whether the file has sound."""
    out = _run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
        str(path),
    ], timeout=60)  # fmt: skip
    info = json.loads(out.stdout or b"{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    duration = float((info.get("format") or {}).get("duration") or 0)
    if video is None:
        raise AssembleError(f"{path.name} has no video stream")
    return {"width": int(video["width"]), "height": int(video["height"]),
            "duration_s": duration, "audio": audio}  # fmt: skip


def assemble(clips: list[Path], out: Path, poster: Path | None = None) -> dict[str, Any]:
    """Concatenate `clips` into `out` (mp4, 24 fps, AAC sound), and write the first frame
    to `poster` (JPEG) if asked. Returns {duration_s, width, height, clips}."""
    if not clips:
        raise AssembleError("there are no clips to assemble")
    infos = [probe(p) for p in clips]
    w, h = infos[0]["width"], infos[0]["height"]
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in clips:
        args += ["-i", str(p)]
    # A clip without sound gets a silent track, so the concat has sound throughout.
    silent = [i for i, info in enumerate(infos) if not info["audio"]]
    for _ in silent:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    filters, pairs = [], []
    for i, info in enumerate(infos):
        filters.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]"
        )
        if info["audio"]:
            filters.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            src = len(clips) + silent.index(i)
            filters.append(f"[{src}:a]atrim=0:{info['duration_s']:.3f},asetpts=PTS-STARTPTS[a{i}]")
        pairs.append(f"[v{i}][a{i}]")
    filters.append(f"{''.join(pairs)}concat=n={len(clips)}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
             str(out)]  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(args)
    if poster is not None:
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out),
              "-frames:v", "1", "-q:v", "3", str(poster)], timeout=120)  # fmt: skip
    done = probe(out)
    return {"duration_s": done["duration_s"], "width": w, "height": h, "clips": len(clips)}


def poster_of(clip: Path, poster: Path) -> None:
    """The first frame of a clip as a JPEG."""
    poster.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
          "-frames:v", "1", "-q:v", "3", str(poster)], timeout=120)  # fmt: skip
