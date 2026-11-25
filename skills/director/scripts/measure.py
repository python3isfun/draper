#!/usr/bin/env python3
"""Measure a vertical drama clip for a director's review: cuts, shot lengths, loudness and a
contact sheet with one frame per shot.

    python skills/director/scripts/measure.py VIDEO [VIDEO ...] [--out DIR] [--threshold 0.25]
                                              [--min-shot 0.3] [--no-sheet]

For each video it prints a summary and writes to --out/<video name>/ (default: the system
temp folder's director-measure/, so a third-party clip's frames never land in the repo):

- shots.json: the shots (start, end, length), the stats, the loudness;
- shots.jpg: a contact sheet, one frame from the middle of each shot, labeled
  "<n> <start>s +<length>" so findings can cite shot numbers.

Cut detection is ffmpeg's scene score, which also fires on a big light change inside one
shot (a walk past a window) and can miss a cut between two similar frames. Look at the
contact sheet and merge or split by eye; pass --merge "45.04,45.21" to drop detections.

Needs ffmpeg/ffprobe and ImageMagick (`magick`). No drawtext filter is needed.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed: {out.stderr.strip()[-400:]}")
    return out.stdout + out.stderr


def probe(video: Path) -> dict:
    raw = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", str(video)])  # fmt: skip
    info = json.loads(raw[raw.index("{") :])
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    num, _, den = v.get("r_frame_rate", "0/1").partition("/")
    return {
        "duration_s": float(info["format"]["duration"]),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": round(int(num) / int(den or 1), 3) if den != "0" else None,
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


def cuts(video: Path, threshold: float) -> list[float]:
    log = run(["ffmpeg", "-hide_banner", "-i", str(video), "-vf",
               f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])  # fmt: skip
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", log)]


def loudness(video: Path) -> dict:
    log = run(["ffmpeg", "-hide_banner", "-i", str(video), "-af", "volumedetect", "-vn",
               "-f", "null", "-"])  # fmt: skip
    mean = re.search(r"mean_volume: (-?[0-9.]+) dB", log)
    peak = re.search(r"max_volume: (-?[0-9.]+) dB", log)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "peak_db": float(peak.group(1)) if peak else None,
    }


def shots_from(cut_times: list[float], end: float, min_shot: float) -> list[dict]:
    """Shots between cuts; a detection closer than min_shot to the last cut is dropped."""
    bounds = [0.0]
    for t in sorted(cut_times):
        if t - bounds[-1] >= min_shot and end - t >= min_shot:
            bounds.append(t)
    bounds.append(end)
    return [
        {"n": i, "start": round(a, 2), "end": round(b, 2), "length": round(b - a, 2)}
        for i, (a, b) in enumerate(itertools.pairwise(bounds), 1)
    ]


def stats(shots: list[dict]) -> dict:
    lengths = [s["length"] for s in shots]
    n = len(lengths)
    return {
        "shots": n,
        "mean_s": round(statistics.mean(lengths), 2),
        "median_s": round(statistics.median(lengths), 2),
        "longest_s": round(max(lengths), 2),
        "over_3s": sum(x > 3 for x in lengths),
        "over_4s": sum(x > 4 for x in lengths),
        "over_4s_share": round(sum(x > 4 for x in lengths) / n, 2),
        "cuts_per_minute": round(n / (sum(lengths) / 60), 1),
    }


def sheet(video: Path, shots: list[dict], out: Path, width: int = 160) -> Path:
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for s in shots:
        f = frames / f"shot_{s['n']:03d}.png"
        mid = (s["start"] + s["end"]) / 2
        run(["ffmpeg", "-v", "error", "-ss", f"{mid:.3f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={width}:-1", "-y", str(f)])  # fmt: skip
        args += ["-label", f"{s['n']} {s['start']:.1f}s +{s['length']:.1f}", str(f)]
    target = out / "shots.jpg"
    run(["magick", "montage", *args, "-tile", "10x", "-geometry", "+2+2", "-pointsize", "14",
         str(target)])  # fmt: skip
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("videos", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "director-measure")
    p.add_argument("--threshold", type=float, default=0.25, help="scene score for a cut")
    p.add_argument("--min-shot", type=float, default=0.3, help="shortest shot, seconds")
    p.add_argument("--merge", default="", help="comma-separated cut times to ignore")
    p.add_argument("--no-sheet", action="store_true", help="skip the contact sheet")
    args = p.parse_args(argv)
    ignore = [float(x) for x in args.merge.split(",") if x.strip()]

    for video in args.videos:
        if not video.is_file():
            print(f"{video}: not found", file=sys.stderr)
            return 1
        info = probe(video)
        found = [t for t in cuts(video, args.threshold)
                 if not any(abs(t - m) < 0.05 for m in ignore)]  # fmt: skip
        shots = shots_from(found, info["duration_s"], args.min_shot)
        result = {"video": str(video), **info, "stats": stats(shots),
                  "loudness": loudness(video) if info["has_audio"] else None,
                  "shots": shots}  # fmt: skip
        out = args.out / video.stem
        out.mkdir(parents=True, exist_ok=True)
        if not args.no_sheet:
            result["sheet"] = str(sheet(video, shots, out))
        (out / "shots.json").write_text(json.dumps(result, indent=2) + "\n")

        s, loud = result["stats"], result["loudness"] or {}
        print(f"{video.name}: {info['duration_s']:.1f} s, {info['width']}x{info['height']}, "
              f"{info['fps']} fps")  # fmt: skip
        print(f"  shots {s['shots']}, mean {s['mean_s']} s, median {s['median_s']} s, longest "
              f"{s['longest_s']} s, over 4 s: {s['over_4s']} ({s['over_4s_share']:.0%}), "
              f"{s['cuts_per_minute']} cuts/min")  # fmt: skip
        if loud:
            peak = loud["peak_db"]  # 0.0 dB is a value, and the loudest one
            clip = "  (at full scale: check for clipping)" if peak is not None and peak > -0.5 else ""
            print(f"  loudness mean {loud['mean_db']} dB, peak {loud['peak_db']} dB{clip}")
        print(f"  -> {out}/shots.json" + ("" if args.no_sheet else f", {out}/shots.jpg"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
