"""The speech check of a rendered clip: who is seen talking, cut by cut, against the plan.

H3 lip-syncs a quoted line to the face it sees, so a clip can put a line on the wrong
person, or make someone talk in a cut that should be silent. After a render, frames are
sampled over every line (and over a silent cut) and shown to the vision model with the
faces of the shot's people; it says whose mouth moves in each cut. The findings are
rules on its answer: an on-screen line is spoken by its speaker and nobody else, a cut
with no line in frame has nobody talking. A take that fails is rejected: it isn't counted
as a take, the episode isn't cut from it, and the run renders another take with a new
seed (then holds for a person, as a failed render does).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..llm import GenerationRequest
from .models import CheckItem, SubShot
from .plans import sub_shot_lines

SPEECH_STAGE = "speech_check"

# Frames looked at per line, spread over the line, and per silent cut.
FRAMES_PER_LINE = 3
FRAMES_PER_SILENT_CUT = 2
MAX_FRAMES = 12
FRAME_SHORT_EDGE = 512

SPEECH_ROLE = """\
You check the dialogue of one rendered clip of a short drama: who is seen talking. You \
look at frames sampled from the clip, cut by cut, and at the faces of the people in the \
scene, and you say whose mouth is moving as if speaking in each cut. Be literal: report \
what the frames show, not what the plan says should happen.\
"""

SPEECH_GUIDE = """\
## Your input

- `people`: the people of the shot, each with the `<Picture N>` that shows their face \
(attached first). Identify people in the frames by these faces.
- `cuts`: the clip's cuts in order, each with its frames (`<Cut K frame J>`, attached \
in that order), who the plan puts in frame, and the lines spoken during it, for context \
only.

## What you look for

For each cut, the people whose mouth is open or moving across its frames as if \
speaking: lips parted differently from frame to frame, a jaw working, teeth or tongue \
visible mid-word. A closed mouth, a smile, a held open mouth of surprise, eating or \
drinking are not speaking. Name each speaking person by the `people` entry whose face \
matches; a speaking person who matches none of them is "someone else".

Return {"cuts": [{"sub_shot": K, "speaking": ["name", ...], "note": "..."}, ...], \
"summary": "..."}: one entry per cut, `speaking` empty when nobody in the frames is \
speaking, `note` what you saw in one sentence (which frames, which face).\
"""


class CutSpeech(BaseModel):
    sub_shot: int
    speaking: list[str] = Field(default_factory=list)
    note: str = ""


class SpeechOutput(BaseModel):
    cuts: list[CutSpeech]
    summary: str = ""


class ClipCheck(BaseModel):
    """What the speech check found in a rendered clip."""

    items: list[CheckItem]
    summary: str = ""
    passed: bool
    model: str | None = None
    checked_at: datetime | None = None
    frames: int = 0  # frames looked at


def speech_instructions() -> str:
    return SPEECH_ROLE + "\n\n" + SPEECH_GUIDE


def sample_times(subs: list[SubShot], lines: list[dict[str, Any]]) -> list[tuple[int, list[float]]]:
    """Where to sample the clip, per cut: `FRAMES_PER_LINE` times spread over every line
    spoken in the cut, else `FRAMES_PER_SILENT_CUT` over the cut; `MAX_FRAMES` in all,
    trimmed from the longest lists first. Times in seconds from the start of the clip."""
    out: list[tuple[int, list[float]]] = []
    for sub in subs:
        said = [x for x in lines if sub.start_ms <= x["start_ms"] < sub.end_ms]
        times: list[float] = []
        if said:
            for x in said:
                start, end = x["start_ms"], min(x["end_ms"], sub.end_ms)
                if end <= start:
                    end = min(start + 1000, sub.end_ms)
                times += _spread(start, end, FRAMES_PER_LINE)
        else:
            times += _spread(sub.start_ms, sub.end_ms, FRAMES_PER_SILENT_CUT)
        out.append((sub.index, sorted(set(times))))
    while sum(len(t) for _, t in out) > MAX_FRAMES:
        k = max(range(len(out)), key=lambda i: len(out[i][1]))
        index, times = out[k]
        out[k] = (index, times[:-1] if len(times) > 1 else times)
        if all(len(t) <= 1 for _, t in out):
            break
    return out


def _spread(start_ms: int, end_ms: int, n: int) -> list[float]:
    """`n` times inside [start, end), away from both edges (a line's first and last frames
    often show a closed mouth)."""
    span = end_ms - start_ms
    return [round((start_ms + span * (k + 1) / (n + 1)) / 1000, 3) for k in range(n)]


def speech_context(
    names: dict[str, str],
    people: list[str],
    subs: list[SubShot],
    lines: list[dict[str, Any]],
    samples: list[tuple[int, list[float]]],
    faces: dict[str, int],
) -> dict[str, Any]:
    """What the model is told: the people with their face pictures, the cuts with their
    frames, who is in frame and the lines spoken (`faces`: person id -> picture number)."""
    return {
        "people": [
            {"name": names.get(p, p), "face": f"<Picture {faces[p]}>" if p in faces else None}
            for p in people
        ],
        "cuts": [
            {
                "sub_shot": sub.index,
                "start_s": round(sub.start_ms / 1000, 3),
                "end_s": round(sub.end_ms / 1000, 3),
                "frames": [f"<Cut {sub.index} frame {j}>" for j in range(1, len(times) + 1)],
                "in_frame": [names.get(p, p) for p in sub.people],
                "lines": [
                    {"speaker": x["speaker"], "line": x["text"], "where": x["where"]}
                    for x in sub_shot_lines(lines, sub)
                ],
            }
            for sub, (_, times) in zip(subs, samples, strict=True)
        ],
    }


def speech_findings(out: SpeechOutput, cuts: list[dict[str, Any]]) -> list[CheckItem]:
    """The check's items from the model's answer: for an on-screen line, its speaker is
    seen speaking (minor when nobody is: a sampled frame can miss the words) and nobody
    else is (major); a cut with no line spoken in frame has nobody speaking (major)."""
    seen_by = {c.sub_shot: c for c in out.cuts}
    items: list[CheckItem] = []
    for cut in cuts:
        k = cut["sub_shot"]
        answer = seen_by.get(k)
        known = list(dict.fromkeys([*cut["in_frame"], *(x["speaker"] for x in cut["lines"])]))
        seen = _match(answer.speaking if answer else [], known)
        note = (answer.note.strip() if answer else "") or "not reported"
        shown = [x for x in cut["lines"] if x["where"] == "on screen"]
        if shown:
            speakers = {x["speaker"] for x in shown}
            for who in dict.fromkeys(x["speaker"] for x in shown):
                items.append(CheckItem(
                    item=f"cut {k}: {who} speaks", kind="action", passed=who in seen,
                    severity="minor",
                    finding="" if who in seen else f"{who} isn't seen speaking ({note})",
                ))  # fmt: skip
            wrong = [w for w in seen if w not in speakers]
            items.append(CheckItem(
                item=f"cut {k}: nobody else speaks", kind="action", passed=not wrong,
                severity="major",
                finding="" if not wrong else (
                    f"{_and(wrong)} {'is' if len(wrong) == 1 else 'are'} seen speaking on "
                    f"{_and(sorted(speakers))}'s line ({note})"),
            ))  # fmt: skip
        else:
            why = "the line is spoken off screen" if cut["lines"] else "the cut has no line"
            items.append(CheckItem(
                item=f"cut {k}: silent in frame", kind="action", passed=not seen,
                severity="major",
                finding="" if not seen else f"{_and(seen)} {'is' if len(seen) == 1 else 'are'} "
                f"seen speaking, but {why} ({note})",
            ))  # fmt: skip
    return items


def face_owner(about: str, names: dict[str, str]) -> str | None:
    """Whose face a clip's picture shows, from what the picture is about ("Lila Montgomery",
    "Lila Montgomery's face"): the character id, or None for a picture of nobody known."""
    about = about.strip()
    for who, name in names.items():
        if about == name or about.startswith((f"{name}'s ", f"{name} ")):
            return who
    return None


def _match(seen: list[str], known: list[str]) -> list[str]:
    """The names the model gave, as the plan's names where they match (the same name in
    another case first, else the same first name); others as given."""
    out: list[str] = []
    for name in seen:
        clean = name.strip()
        if not clean:
            continue
        hit = next((p for p in known if p.lower() == clean.lower()), None) or next(
            (p for p in known if _first(p) == _first(clean)), clean
        )
        if hit not in out:
            out.append(hit)
    return out


def _first(name: str) -> str:
    return re.split(r"[\s,]+", name.strip().lower())[0]


def _and(names: list[str]) -> str:
    return " and ".join(names)


def fixture_speech(request: GenerationRequest) -> SpeechOutput:
    """Offline stand-in: everyone speaks their on-screen lines and nobody else does."""
    cuts = [
        CutSpeech(sub_shot=c["sub_shot"],
                  speaking=list(dict.fromkeys(x["speaker"] for x in c["lines"]
                                              if x["where"] == "on screen")),
                  note="Offline sample: not looked at.")
        for c in request.context.get("cuts", [])
    ]  # fmt: skip
    return SpeechOutput(cuts=cuts, summary="Offline sample: not looked at.")
