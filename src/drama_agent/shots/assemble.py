"""Assembling an episode: its shots' clips, one after another, into one file (ffmpeg).

The clips are concatenated in storyboard order with their own sound; each is scaled and
padded to the first clip's size and re-encoded, so clips of different sizes or codecs join
cleanly. Each clip can be trimmed to its sound (`trim_points`: a rendered clip warms up
from a posed first frame and settles into a held end state; the dead air is cut) and a
music bed can be laid under the scenes (`music`), mixed below the dialogue. A line can be
heard over the picture of the clip beside the one that speaks it (`overlaps`, `lay_over`).
No titles or transitions: the cut is the editor's; this is the rough assembly that shows the episode as
rendered.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Trimming: sound quieter than this, for this long, is silence.
SILENCE_DB = -35.0
SILENCE_S = 0.3
TRIM_LEAD_S = 0.25  # kept before the first sound
TRIM_TAIL_S = 0.6  # kept after the last sound
TRIM_KEEP = 0.5  # a clip is never cut below this share of its length (an action clip)
MUSIC_DB = -16.0  # the music bed's level under the dialogue
MUSIC_FADE_S = 1.5


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


def sound_window(
    path: Path, noise_db: float = SILENCE_DB, min_silence_s: float = SILENCE_S
) -> tuple[float, float] | None:
    """(first, last) seconds at which the clip's sound is above `noise_db`, from ffmpeg's
    silence detection; None when the clip has no sound track or no sound above the floor."""
    info = probe(path)
    if not info["audio"]:
        return None
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af",
         f"silencedetect=n={noise_db}dB:d={min_silence_s}", "-f", "null", "-"],
        check=False, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    log = out.stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    duration = info["duration_s"]
    first = 0.0
    if starts and starts[0] <= 0.05:  # silent from the top: sound begins where it ends
        if not ends:
            return None  # silent throughout
        first = ends[0]
    last = duration
    # Silent to the end: the last silence has no end, or ends at the file's end (ffmpeg
    # reports the end of a silence still open at EOF).
    if starts and (len(starts) > len(ends) or ends[-1] >= duration - 0.1):
        last = starts[-1]
    return (first, last) if last > first + 0.05 else None


def trim_points(
    path: Path,
    lead_s: float = TRIM_LEAD_S,
    tail_s: float = TRIM_TAIL_S,
    keep: float = TRIM_KEEP,
) -> tuple[float, float]:
    """Where to cut a clip in and out: `lead_s` before its first sound to `tail_s` after
    its last, never shorter than `keep` of its length (a clip that is mostly silent is an
    action clip: it keeps its tail). (0, duration) when the sound can't be read."""
    duration = probe(path)["duration_s"]
    window = sound_window(path)
    if window is None:
        return 0.0, duration
    start = max(0.0, window[0] - lead_s)
    end = min(duration, window[1] + tail_s)
    floor = duration * keep
    if end - start < floor:
        end = min(duration, start + floor)
        if end - start < floor:
            start = max(0.0, end - floor)
    return round(start, 3), round(end, 3)


@dataclass(frozen=True)
class MusicBed:
    """A music file laid under the episode from `start_s` for `length_s` (faded in and out)."""

    path: Path
    start_s: float
    length_s: float
    gain_db: float = MUSIC_DB


OVER_PAD_S = 0.3  # kept around a line's words laid over the next shot's picture
OVER_SHARE = 0.6  # a clip gives at most this share of its sound to the shots beside it
PEAK = 0.89  # the mix's ceiling (about -1 dB)


def lay_over(
    trims: list[tuple[float, float]],
    durations: list[float],
    heard: list[list[tuple[str, float]]],
    speaks: list[bool],
    planned: list[float],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Trims and overlaps for clips whose shots hear a line spoken in the clip beside them
    (plans.heard_lines). `heard[i]` is, per such line of clip i, (side, seconds): "before"
    (the clip before speaks it: its sound runs on over this clip's picture) or "after"
    (the clip after speaks it: its sound starts under this clip's picture). `speaks[i]`
    says whether clip i has a line of its own; `planned[i]` is its shot's length.
    Returns (trims, overlaps): overlaps[i] is the (head, tail) seconds of clip i's sound
    heard over its neighbours' picture instead of its own."""
    cut = [list(t) for t in trims]
    over = [[0.0, 0.0] for _ in trims]
    for i, lines in enumerate(heard):
        for side, seconds in lines:
            if side == "before" and i > 0:
                room = OVER_SHARE * (cut[i - 1][1] - cut[i - 1][0])
                tail = min(seconds + OVER_PAD_S, room)
                over[i - 1][1] = max(over[i - 1][1], tail)
                # This clip shows the listener for that long before its own sound.
                cut[i][0] = max(0.0, cut[i][0] - tail)
                if not speaks[i]:  # a listener's clip is as long as the storyboard made it
                    cut[i][1] = min(durations[i], cut[i][0] + max(planned[i], tail + 0.5))
            elif side == "after" and i + 1 < len(trims):
                room = OVER_SHARE * (cut[i + 1][1] - cut[i + 1][0])
                head = min(seconds, room)
                over[i + 1][0] = max(over[i + 1][0], head)
                # The words start under this clip's end: it holds for them.
                cut[i][1] = min(durations[i], cut[i][1] + max(0.0, head - TRIM_TAIL_S / 2))
    return ([(round(a, 3), round(b, 3)) for a, b in cut],
            [(round(a, 3), round(b, 3)) for a, b in over])  # fmt: skip


def assemble(
    clips: list[Path],
    out: Path,
    poster: Path | None = None,
    trims: list[tuple[float, float]] | None = None,
    music: list[MusicBed] | None = None,
    overlaps: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Concatenate `clips` into `out` (mp4, 24 fps, AAC sound), and write the first frame
    to `poster` (JPEG) if asked. `trims` cuts each clip to (in, out) seconds; `music` lays
    beds under the result. `overlaps` gives per clip the (head, tail) seconds of its sound
    that play over the picture of the clip before and after it (an editor's J and L cuts):
    its picture is that much shorter than its sound. The mix is limited to about -1 dB.
    Returns {duration_s, width, height, clips, trimmed_s}."""
    if not clips:
        raise AssembleError("there are no clips to assemble")
    infos = [probe(p) for p in clips]
    trims = trims or [(0.0, info["duration_s"]) for info in infos]
    if len(trims) != len(clips):
        raise AssembleError("one trim per clip")
    overlaps = overlaps or [(0.0, 0.0)] * len(clips)
    if len(overlaps) != len(clips):
        raise AssembleError("one overlap per clip")
    w, h = infos[0]["width"], infos[0]["height"]
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in clips:
        args += ["-i", str(p)]
    # A clip without sound gets a silent track, so the mix has sound throughout.
    silent = [i for i, info in enumerate(infos) if not info["audio"]]
    for _ in silent:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    music = music or []
    for bed in music:
        args += ["-i", str(bed.path)]
    filters, pictures, sounds = [], [], []
    at = 0.0  # where the clip's picture starts in the episode
    for i, (info, (t0, t1), (head, tail)) in enumerate(zip(infos, trims, overlaps, strict=True)):
        if t1 - t0 - head - tail < 0.1:  # never a clip with no picture
            head = tail = 0.0
        v0, v1 = t0 + head, t1 - tail
        filters.append(
            f"[{i}:v]trim={v0:.3f}:{v1:.3f},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]"
        )
        # The first clip has nothing before it to play over: its sound starts at its picture.
        a0 = v0 if at - head < 0 else t0
        delay = round(max(0.0, at - (v0 - a0)) * 1000)
        if info["audio"]:
            src, cut = f"[{i}:a]", f"atrim={a0:.3f}:{t1:.3f}"
        else:
            src, cut = f"[{len(clips) + silent.index(i)}:a]", f"atrim=0:{t1 - a0:.3f}"
        filters.append(f"{src}{cut},asetpts=PTS-STARTPTS,aresample=48000,"
                       f"aformat=channel_layouts=stereo,adelay={delay}|{delay}[a{i}]")  # fmt: skip
        pictures.append(f"[v{i}]")
        sounds.append(f"[a{i}]")
        at += v1 - v0
    filters.append(f"{''.join(pictures)}concat=n={len(clips)}:v=1:a=0[v]")
    for k, bed in enumerate(music):
        src = len(clips) + len(silent) + k
        delay = round(bed.start_s * 1000)
        fade_out = max(0.0, bed.length_s - MUSIC_FADE_S)
        filters.append(
            f"[{src}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"atrim=0:{bed.length_s:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={MUSIC_FADE_S},afade=t=out:st={fade_out:.3f}:d={MUSIC_FADE_S},"
            f"volume={bed.gain_db:.1f}dB,adelay={delay}|{delay}[m{k}]"
        )
        sounds.append(f"[m{k}]")
    filters.append(
        f"{''.join(sounds)}amix=inputs={len(sounds)}:duration=longest:normalize=0,"
        f"atrim=0:{at:.3f},alimiter=limit={PEAK}:level=disabled[a]"
    )
    args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
             str(out)]  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(args)
    if poster is not None:
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out),
              "-frames:v", "1", "-q:v", "3", str(poster)], timeout=120)  # fmt: skip
    done = probe(out)
    trimmed = sum(
        info["duration_s"] - (t1 - t0) for info, (t0, t1) in zip(infos, trims, strict=True)
    )
    return {"duration_s": done["duration_s"], "width": w, "height": h, "clips": len(clips),
            "trimmed_s": round(max(0.0, trimmed), 3), "music": len(music)}  # fmt: skip


def shrink_picture(path: Path, short_edge: int, quality: int = 3) -> bytes | None:
    """The picture as a JPEG whose short edge is at most `short_edge` (its aspect kept),
    or None when it is already that small, so the caller sends it as it is. `quality` is
    ffmpeg's JPEG scale (2 best, 31 worst)."""
    info = probe(path)
    if min(info["width"], info["height"]) <= short_edge:
        return None
    scale = f"scale=w='if(lt(iw,ih),{short_edge},-2)':h='if(lt(iw,ih),-2,{short_edge})'"
    out = _run([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf", scale, "-frames:v", "1",
        "-q:v", str(quality), "-f", "image2", "-c:v", "mjpeg", "pipe:1",
    ], timeout=120)  # fmt: skip
    return out.stdout


def frame_at(clip: Path, at_s: float, out: Path, short_edge: int | None = None) -> None:
    """The frame of a clip at `at_s` seconds as a JPEG, shrunk to `short_edge` if given."""
    out.parent.mkdir(parents=True, exist_ok=True)
    scale = ([] if short_edge is None else
             ["-vf", f"scale='if(gt(iw,ih),-2,{short_edge})':'if(gt(iw,ih),{short_edge},-2)'"])  # fmt: skip
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at_s:.3f}",
          "-i", str(clip), "-frames:v", "1", *scale, "-q:v", "3", str(out)], timeout=120)  # fmt: skip


def poster_of(clip: Path, poster: Path) -> None:
    """The first frame of a clip as a JPEG."""
    poster.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
          "-frames:v", "1", "-q:v", "3", str(poster)], timeout=120)  # fmt: skip
