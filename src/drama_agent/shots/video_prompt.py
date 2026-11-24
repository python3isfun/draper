"""The H3 video prompt of a shot: what the text model is told and given, the rules a
program can check, and the offline template.

Video production (dramer) renders each shot's clip with MiniMax H3 from a prompt and the
shot's pictures (clip_refs.py). The prompt is written here, from the same plan, keyframes
and scene reading the keyframes were drawn from, so it can't drift from them. The plan
drafts the keyframes; once a cut has an approved keyframe, that picture drives its prompt:
the writer sees it and describes the cut as it shows it (framing, camera position, who is
where), with the plan giving the action, timing and lines. It follows
dramer's H3 video prompt skill: where the camera stands before the set; every person bound
to a picture and their clothes in one sentence; every line quoted word for word with
"says in English:" and whose lips move; only what is there, never what isn't; time the
action to the clip; one camera sentence in MiniMax wording; one sound sentence.

The prompt is one block of text per cut (no timestamps: dramer writes "[Shot N] At
<timecode>" from the plan), each with its camera sentence as its own field, and one sound
sentence for the shot. Pictures are named `{pic:<id>}`: the model sees short ids (A, B,
...), stored prompts hold the picture's ref id, the export its file name, and dramer turns
each into `<Picture N>` in the order it sends.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel

from ..llm import GenerationRequest
from ..models import ProductionPackage
from ..models.images import GeneratedImage
from ..models.storyboard import Shot
from ..shot_reading import SceneReading
from .approvals import approved_take, frame_ref
from .clip_refs import clip_references, exportable
from .common import _dialogue, _names
from .models import PromptBlock, ShotPlan, ShotTake, ShotVideoPrompt
from .plans import current_sub, shot_lines, sub_shot_lines
from .references import chosen_pictures
from .writer import shot_context, sub_shot_context

VIDEO_PROMPT_STAGE = "video_prompt"

PROMPT_FORMAT = "h3-paragraph/1"

WORDS_PER_S = 2.5  # a slow drama read
MIN_WORDS, MAX_WORDS = 150, 500
MAX_CHARS = 7000  # H3's hard limit

# MiniMax's camera vocabulary: a camera sentence starts with one of these.
CAMERA_MOVES = (
    "static shot", "push in", "pull out", "pan left", "pan right", "truck left",
    "truck right", "tilt up", "tilt down", "pedestal up", "pedestal down", "arc shot",
    "tracking shot", "zoom in", "zoom out", "shake slightly", "shake strongly", "pov",
    "roll clockwise", "roll counterclockwise",
)  # fmt: skip

VIDEO_ROLE = """\
You write the MiniMax H3 video prompt of one shot of a short drama. H3 renders the shot's \
clip, 4 to 15 s with its own sound, from your prompt and the pictures sent with it. H3 is \
generous and literal: what the prompt leaves unsaid it decides on its own, and what the \
prompt mentions, even negated, it tends to add. Most retakes trace to four omissions: \
where the camera is, the exact spoken words, what is heard, and which picture each person \
is. Close those gaps.\
"""

VIDEO_GUIDE = """\
## Your input

- `shot`: the storyboard shot (its action, continuity, notes, props, the people in it and \
their look), `scene` (place, set layout, light, time of day) and `style`.
- `clip_s`: the clip's length in seconds.
- `pictures`: every picture sent with the clip, in order, each with its id (`{pic:A}`), \
what it shows and its kind: `keyframe` (the approved picture of one cut: the cut matches \
its framing, positions and light; it is a reference, not a start frame), `chosen` or \
`look` (a person in this shot's clothes, full body), `face` (a person's face), `set` (the \
empty set), `prop`; `of` names the person, place or prop shown.
- `cuts`: the shot's cuts in time order, each with `start_s` and `end_s`, its planned \
framing, camera, action, who is on screen and on which side (`frame`), with their state \
now (`now`: wearing, hair, holding, condition), who is off screen and on which side, its \
`keyframe` picture (if any; the picture itself is attached, labelled with its id), what \
the keyframe's own check found wrong with it against the plan (`keyframe_check`), and its \
`lines` (speaker, words, delivery, and `where`: "on screen", "off screen <side>" or \
"voice").

## The keyframe drives the cut

The plan was written first and the keyframe drawn from it; the keyframe is what the clip \
will match, so **where a cut has a keyframe, describe the cut as the keyframe shows it**: \
look at the attached picture and take the shot size, the camera's position and height, \
what it faces, who is where in the frame, their pose, clothes and what is behind them \
from the picture, not from the plan's words. `keyframe_check` lists where the plan and \
the picture are known to differ; follow the picture there. The plan still gives what \
happens in the cut (its action, timing, lines) and the camera's movement. When a \
keyframe differs from the plan in a way the story needs fixed (the wrong person, a prop \
missing, a side flipped), say so in `notes` so the keyframe can be redrawn. A cut with \
no keyframe is written from the plan.
- `carried`: lines begun in the shot before, still being finished as this clip opens.
- On a second try: `previous` (your prompt) and `problems` (what a check found). Fix \
exactly those.

## What you write

One `blocks` entry per cut, in cut order, each with `sub_shot` (its number), `text` and \
`camera`; then `sound` and `notes`.

`text` is the cut in plain English prose, written in this order:

1. **Framing and camera position first**, as the keyframe shows them: shot size, where \
the camera stands, its height, what it faces ("A medium two-shot from the aisle, camera \
at chest height facing the booth"). A reverse or over-the-shoulder angle is geometry ("camera behind his right \
shoulder, looking past him toward her").
2. **Pictures, each with its role.** For the cut's keyframe: "{pic:A} is the reference \
frame of this cut: it matches its framing, positions and light." For the set, once in the \
first cut where it is seen: what it keeps (architecture, furniture placement, palette).
3. **Each person in frame, bound to their pictures, dressed and placed, in one sentence** \
(a person off screen in the cut is never bound to a picture there: H3 draws whoever is \
described): "the \
woman from {pic:B} and {pic:C}, in an ivory blouse and charcoal pants, standing at frame \
left". A name alone means nothing to H3; give the picture every time a person is \
introduced in a cut. The clothes and the screen side are what the keyframe shows (else \
`now.wearing` and the cut's `frame`).
4. **Setting and light, briefly.** With a keyframe for the cut, don't take inventory of \
the set: the picture shows it. In a close-up, say what is behind the face.
5. **Action as timed verbs** covering the cut from its start to its end, in seconds from \
the start of the clip ("from 2 to 3.5 s she turns toward the door"), and the end state \
the next cut picks up from. One beat fills 5 to 8 s; don't pad a small action.
6. **The lines**, word for word, in order, in double quotes, after "says in English:", \
the speaker on screen while speaking: `the man from {pic:D} says in English: "Took you \
long enough." Only his lips move; the woman keeps quiet, mouth closed.` Delivery goes \
outside the quotes. Every speaking verb has its quoted line. A cut with no line says so \
in the positive: "Both stay silent, mouths closed." **A line whose speaker is off screen \
(or a voice from outside the shot) while anyone is in frame is not quoted in `text` and \
never binds the speaker to a picture**: H3 lip-syncs a quoted line to the face it sees, \
so the words go in `sound` as voice-over (`Off screen, Lila's voice, dry: "..."`) and \
`text` says in the positive that the people in frame listen in silence, mouths closed, \
with the speaker named only as a direction ("facing Lila, who is off frame left"). With \
nobody in frame the line is quoted in `text` after "says in an off-screen voice:".

`camera`: one sentence in MiniMax's camera vocabulary for this cut: static shot, push \
in, pull out, pan left/right, truck left/right, tilt up/down, pedestal up/down, arc shot, \
tracking shot, zoom in/out, shake slightly/strongly, adding "with small/large amplitude" \
and "at slow/fast speed" only when they matter (e.g. "push in with small amplitude at slow \
speed"). Start it with the movement's name. It is sent separately: don't repeat it in \
`text`.

`sound`: one sentence, the last of the prompt: "Apart from the spoken lines, the only \
sounds are ..." naming the place's ambience and the action's sounds. When nobody speaks: \
"Everyone stays silent; the only sounds are ...".

`notes`: what this shot needs that no prompt can give (a person the action names who has \
no picture, a sign that must read a certain way), or "".

## Rules

- Name every picture in `pictures` at least once, as `{pic:X}` exactly, and only those.
- Speak only of the people in the shot. A person the action mentions who isn't in it is \
left out entirely, not even "off frame": H3 draws whoever is described. Write an eyeline \
as a direction ("his eyes on a point just right of the lens").
- Describe what is there, never what is not: no "no", "not", "without", "never", \
"avoid" about the scene. H3 has no negative prompt; a ban adds the banned thing.
- Concrete over abstract: name the light, the lens height, the gesture; not "cinematic", \
"beautiful", "emotional".
- Quote any sign or writing that must read; keep other writing out of focus.
- Lines spoken in the shot before (`carried`) aren't spoken again: say the voice carries \
over as the clip opens.
- No timestamps like "[Shot 2] At 00:02.000": they are added from the plan.
- 150 to 500 words in all (`text` of every cut, `camera` and `sound`).
"""


class VideoPromptOutput(BaseModel):
    blocks: list[PromptBlock]
    sound: str
    notes: str = ""


def video_instructions() -> str:
    return VIDEO_ROLE + "\n\n" + VIDEO_GUIDE


def approved_frames(
    pkg: ProductionPackage, shot: Shot, plan: ShotPlan, takes: list[ShotTake]
) -> dict[int, str | None]:
    """Each cut of the plan in force with its approved keyframe (frame:<take>:<n>), or
    None. Empty for the storyboard's own single cut without a keyframe: nothing to show."""
    frames = {}
    for sub in plan.sub_shots:
        t = approved_take(takes, shot.shot_id, sub.index)
        ok = t is not None and current_sub(pkg, shot, plan, t) is not None
        frames[sub.index] = frame_ref(t) if ok and t is not None else None
    return frames if plan.source != "single" or any(frames.values()) else {}


def carried_lines(pkg: ProductionPackage) -> set[tuple[str, str]]:
    """(shot, line) for each line that runs over a cut into a later shot: it is spoken in
    the shot it starts in, and only carried over in the next."""
    out: set[tuple[str, str]] = set()
    for ep in pkg.episodes:
        spoken: set[str] = set()
        for shot in sorted((s for s in pkg.shot_list if s.episode_id == ep.episode_id),
                           key=lambda s: s.start_ms):  # fmt: skip
            for cue in shot.dialogue_cues:
                if cue.dialogue_id in spoken:
                    out.add((shot.shot_id, cue.dialogue_id))
                spoken.add(cue.dialogue_id)
    return out


def _label(i: int) -> str:
    return chr(ord("A") + i)


def prompt_context(
    pkg: ProductionPackage,
    shot: Shot,
    plan: ShotPlan,
    frames: dict[int, str | None],
    reading: SceneReading | None,
    images: list[GeneratedImage],
    usable: Any,
    carried: set[tuple[str, str]],
    checks: dict[int, list[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """(what the writer is given, each picture's short id -> its ref id). The pictures are
    those the clip is sent with (clip_references, with `usable`); `checks` is what each
    cut's keyframe check found wrong (frame_findings), left out of the fingerprint since
    the keyframe's id already stands for it."""
    names = _names(pkg)
    script = _dialogue(pkg)
    refs, _, _ = clip_references(pkg, shot, plan, frames, reading, images, usable)
    ids = {r["ref_id"]: _label(i) for i, r in enumerate(refs)}
    pictures = []
    for r in refs:
        of = names.get(r["entity_id"], "") if r["entity_id"] else ""
        pictures.append({"pic": f"{{pic:{ids[r['ref_id']]}}}", "kind": r["kind"],
                         "of": of, "shows": r["about"], "cuts": r["sub_shots"]})  # fmt: skip
    base = shot_context(pkg, shot)
    base["shot"].pop("dialogue", None)
    base["shot"] |= {"summary": shot.narrative_function, "sound": shot.sound}
    lines = [x for x in shot_lines(pkg, shot) if (shot.shot_id, x["dialogue_id"]) not in carried]
    cuts = []
    for sub in plan.sub_shots:
        states = {w: r.state for w, r in chosen_pictures(pkg, shot, sub, reading).items()
                  if r.state is not None}  # fmt: skip
        cut = sub_shot_context(pkg, shot, sub, len(plan.sub_shots), states)
        cut["lines"] = [
            {"speaker": x["speaker"], "line": x["text"], "where": x["where"],
             "delivery": getattr(script.get(x["dialogue_id"]), "delivery", "") or ""}
            for x in sub_shot_lines(lines, sub)
        ]  # fmt: skip
        f = frames.get(sub.index)
        cut["keyframe"] = f"{{pic:{ids[f]}}}" if f and f in ids else None
        if cut["keyframe"]:
            cut["keyframe_check"] = (checks or {}).get(sub.index, [])
        cuts.append(cut)
    context = {
        **base,
        "clip_s": round(plan.sub_shots[-1].end_ms / 1000, 3),
        "pictures": pictures,
        "cuts": cuts,
        "carried": [{"speaker": names.get(script[d].character_id, ""), "line": script[d].text}
                    for c in shot.dialogue_cues
                    if (shot.shot_id, d := c.dialogue_id) in carried and d in script],
    }  # fmt: skip
    return context, {v: k for k, v in ids.items()}


def prompt_inputs(
    pkg: ProductionPackage,
    shot: Shot,
    plan: ShotPlan,
    takes: list[ShotTake],
    images: list[GeneratedImage],
    reading: SceneReading | None,
    usable: set[str] | None = None,
    carried: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """prompt_context for the shot as it is now: its plan in force, its approved keyframes
    and the pictures an export carries (`usable` and `carried` can be worked out once for
    many shots: exportable(images), carried_lines(pkg))."""
    ok = usable if usable is not None else exportable(images)
    frames = approved_frames(pkg, shot, plan, takes)
    return prompt_context(pkg, shot, plan, frames, reading, images, ok.__contains__,
                          carried if carried is not None else carried_lines(pkg),
                          frame_findings(takes, frames))  # fmt: skip


def prompt_state(stored: ShotVideoPrompt | None, now: str) -> str:
    """none, writing, failed, current (written from the inputs as they are now: `now`, their
    fingerprint) or outdated."""
    if stored is None:
        return "none"
    if stored.status != "done":
        return stored.status
    return "current" if stored.written_from == now else "outdated"


def frame_findings(takes: list[ShotTake], frames: dict[int, str | None]) -> dict[int, list[str]]:
    """Per cut, what its approved keyframe's check found wrong with it (empty when it
    passed or wasn't checked): the plan and the picture are known to differ there."""
    out: dict[int, list[str]] = {}
    for i, ref in frames.items():
        if not ref:
            continue
        _, take_id, n = ref.split(":")
        t = next((t for t in takes if t.take_id == take_id), None)
        d = next((d for d in t.results if d.n == int(n)), None) if t else None
        if d is not None and d.check is not None:
            out[i] = [f"{x.item}: {x.finding}" for x in d.check.items if not x.passed]
    return out


def fingerprint(context: dict[str, Any], ids: dict[str, str]) -> str:
    """What a prompt was written from: it is out of date once this changes."""
    cuts = [{k: v for k, v in c.items() if k != "keyframe_check"} for c in context["cuts"]]
    raw = json.dumps([context | {"cuts": cuts}, sorted(ids.items())], sort_keys=True,
                     ensure_ascii=False)  # fmt: skip
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Checks

PIC = re.compile(r"\{pic:([^{}\s]+)\}")
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]")
_NEGATION = re.compile(r"\b(no|not|never|without|nobody|nothing|avoid|isn't|doesn't|don't)\b",
                       re.IGNORECASE)  # fmt: skip
_SPEAKS = re.compile(
    r"\b(says|said|speaks|asks|replies|answers|whispers|shouts|calls|adds|explains|mutters|"
    r"murmurs|tells|responds)\b",
    re.IGNORECASE,
)
_SILENT = re.compile(r"\b(silent|quiet|mouths? closed|lips? closed)\b", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\[Shot \d+\]|\bAt \d{2}:\d{2}")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z{\"“])", text) if s.strip()]


def word_count(blocks: list[PromptBlock], sound: str) -> int:
    text = " ".join([*(b.text + " " + b.camera for b in blocks), sound])
    return len(PIC.sub("picture", text).split())


def dialogue_pace(
    pkg: ProductionPackage, shot: Shot, clip_ms: int, carried: set[tuple[str, str]] | None = None
) -> str | None:
    """A warning when the lines spoken in the shot (not those carried over from the shot
    before) carry more words than the clip holds at a slow read, else None."""
    carried = carried if carried is not None else carried_lines(pkg)
    words = sum(len(x["text"].split()) for x in shot_lines(pkg, shot)
                if (shot.shot_id, x["dialogue_id"]) not in carried)  # fmt: skip
    room = clip_ms / 1000 * WORDS_PER_S
    if words > room:
        return (f"{words} spoken words in a {clip_ms / 1000:g} s clip: at a slow read "
                f"({WORDS_PER_S:g} words a second) it holds about {int(room)}")  # fmt: skip
    return None


def prompt_findings(
    out: VideoPromptOutput, context: dict[str, Any], others: list[str]
) -> tuple[list[str], list[str]]:
    """(problems, advice) of a written prompt, against what it was written from. Problems
    make it unusable (dramer's gate refuses them); advice usually costs a retake. `others`
    are the story's people not in the shot, who mustn't be named."""
    problems: list[str] = []
    advice: list[str] = []
    cuts = context["cuts"]
    known = {p["pic"][5:-1] for p in context["pictures"]}
    if [b.sub_shot for b in out.blocks] != [c["index"] for c in cuts]:
        problems.append(f"has blocks for cuts {[b.sub_shot for b in out.blocks]}, not one for "
                        f"each of {[c['index'] for c in cuts]} in order")  # fmt: skip
    every = " ".join(b.text for b in out.blocks)
    named = set(PIC.findall(every))
    if unknown := sorted(named - known):
        problems.append(f"names pictures that aren't sent: {', '.join(unknown)}")
    if missing := sorted(known - named):
        problems.append(f"never names {', '.join(f'{{pic:{x}}}' for x in missing)}")
    full = every + " " + " ".join(b.camera for b in out.blocks) + " " + out.sound
    if len(full) > MAX_CHARS:
        problems.append(f"is {len(full)} characters, over H3's {MAX_CHARS}")
    bare = _QUOTED.sub('""', full)
    if found := sorted({n for n in others if re.search(rf"\b{re.escape(n)}\b", bare)}):
        problems.append(f"names {', '.join(found)}, who isn't in the shot: H3 draws whoever "
                        "is described; leave them out")  # fmt: skip
    for block in out.blocks:
        cut = next((c for c in cuts if c["index"] == block.sub_shot), None)
        if cut is None:
            continue
        tag = f"cut {block.sub_shot}"
        at = 0
        shown = [x for x in cut["lines"] if x["where"] == "on screen" or not cut["people"]]
        for x in shown:
            quoted = f'"{x["line"].strip()}"'
            k = block.text.find(quoted, at)
            if k < 0:
                problems.append(f"{tag} doesn't quote {x['speaker']}'s line word for word, "
                                f"in order: {quoted}")  # fmt: skip
            else:
                at = k + len(quoted)
        # A line spoken off screen while someone is in frame is voice-over: in the sound
        # sentence, not in the picture text, which H3 would lip-sync to the face it sees.
        for x in cut["lines"]:
            if x in shown:
                continue
            quoted = f'"{x["line"].strip()}"'
            if quoted in block.text:
                problems.append(f"{tag} quotes {x['speaker']}'s off-screen line in the picture "
                                "text: H3 lip-syncs it to the face in frame. Put it in `sound` "
                                "as voice-over and keep the people in frame silent")  # fmt: skip
            if quoted not in out.sound:
                problems.append(f"`sound` doesn't carry {x['speaker']}'s off-screen line of "
                                f"{tag} word for word: {quoted}")  # fmt: skip
        if shown and "lips" not in block.text.lower():
            advice.append(f"{tag} doesn't say whose lips move on its line")
        if not shown and cut["people"] and not _SILENT.search(block.text):
            advice.append(f"{tag} has no line spoken in frame and doesn't say the people stay "
                          "silent, mouths closed: H3 invents speech")  # fmt: skip
        for person in cut.get("off_screen", []):
            name = person["name"]
            if re.search(rf"\b{re.escape(name)}\b[^.]*\{{pic:", _QUOTED.sub('""', block.text)):
                advice.append(f"{tag} binds {name}, who is off screen in it, to a picture: H3 "
                              "draws whoever is described; name them only as a direction")  # fmt: skip
        for s in _sentences(_QUOTED.sub('""', block.text)):
            if _SPEAKS.search(s) and '""' not in s:
                advice.append(f"{tag} has a speaking verb with no quoted line: {s.strip()[:80]}")
        if words := sorted({m.lower() for m in _NEGATION.findall(_QUOTED.sub("", block.text))}):
            advice.append(f"{tag} says what isn't there ({', '.join(words)}): H3 adds what is "
                          "named; describe what is there")  # fmt: skip
        if _TIMESTAMP.search(block.text):
            advice.append(f"{tag} writes a timestamp; they are added from the plan")
        cam = block.camera.strip().lower()
        if not cam:
            problems.append(f"{tag} has no camera sentence")
        elif not cam.startswith(CAMERA_MOVES) and not cam.startswith(
            tuple(f"the camera {m.split()[0]}" for m in CAMERA_MOVES)
        ):
            advice.append(f"{tag}'s camera isn't in MiniMax's wording: {block.camera[:60]}")
    if "sound" not in out.sound.lower():
        advice.append("has no sound sentence: say what is heard")
    words = word_count(out.blocks, out.sound)
    if words < MIN_WORDS:
        advice.append(f"is {words} words, under {MIN_WORDS}: staging, people or sound is "
                      "usually missing")  # fmt: skip
    elif words > MAX_WORDS:
        advice.append(f"is {words} words, over {MAX_WORDS}")
    return problems, advice


def others_of(pkg: ProductionPackage, shot: Shot) -> list[str]:
    """The story's people who aren't in the shot (on screen or speaking in it)."""
    script = _dialogue(pkg)
    there = set(shot.character_ids) | {
        script[c.dialogue_id].character_id for c in shot.dialogue_cues if c.dialogue_id in script
    }
    return [c.name for c in pkg.story_bible.characters if c.character_id not in there and c.name]


def worse(a: tuple[list[str], list[str]], b: tuple[list[str], list[str]]) -> bool:
    """Whether findings `a` are worse than `b`: more problems, then more advice."""
    return (len(a[0]), len(a[1])) > (len(b[0]), len(b[1]))


def to_refs(text: str, ids: dict[str, str]) -> str:
    """A written prompt's short picture ids as the pictures' ref ids (unknown ones kept)."""
    return PIC.sub(lambda m: f"{{pic:{ids.get(m.group(1), m.group(1))}}}", text)


# ---------------------------------------------------------------------------
# Template (offline, and the shape the model is asked for)

_SIDE = {"left": "at frame left", "center": "in the centre of the frame", "right": "at frame right"}


def template_prompt(context: dict[str, Any]) -> VideoPromptOutput:
    """A prompt built from the plan alone: correct to every rule a program checks, plain
    to read. The offline stand-in for the writer."""
    pics = context["pictures"]
    by_person: dict[str, list[str]] = {}
    for p in pics:
        if p["kind"] in PERSON_KINDS and p["of"]:
            by_person.setdefault(p["of"], []).append(p["pic"])
    sets = [p["pic"] for p in pics if p["kind"] == "set"]
    props = [p for p in pics if p["kind"] == "prop"]
    place = context["scene"].get("place", "the set")
    blocks = []
    voice: list[str] = []
    for k, cut in enumerate(context["cuts"]):
        parts = [
            f"{_cap(cut['framing'].rstrip('.'))} shot, camera at eye height facing the action."
        ]
        if cut.get("keyframe"):
            parts.append(f"{cut['keyframe']} is the reference frame of this cut: it matches "
                         "its framing, positions and light.")  # fmt: skip
        if k == 0:
            if sets:
                parts.append(f"The scene is {place} from {' and '.join(sets)}, its walls, "
                             "furniture placement and palette as shown.")  # fmt: skip
            for p in props:
                parts.append(f"The {p['of'] or 'prop'} is the one from {p['pic']}.")
        for person in cut["people"]:
            mine = by_person.get(person["name"], [])
            wearing = (person.get("now") or {}).get("wearing") or ""
            dressed = f", in {_low(wearing.rstrip('.'))}" if wearing else ""
            source = f" from {' and '.join(mine)}" if mine else ""
            side = _SIDE.get(person.get("frame", ""), "in frame")
            parts.append(f"{person['name']}, the person{source}{dressed}, is {side}.")
        if k == 0:
            # People the shot has pictures of who aren't on screen in the first cut.
            seen = {p["name"] for p in cut["people"]}
            for name, mine in by_person.items():
                if name not in seen and not any(name in {x["name"] for x in c["people"]}
                                                for c in context["cuts"]):  # fmt: skip
                    parts.append(f"{' and '.join(mine)} show {name} as they look in this shot.")
        parts.append(
            f"From {cut['start_s']:g} to {cut['end_s']:g} s, {_low(cut['action'].rstrip('.'))}."
        )
        on = [p["name"] for p in cut["people"]]
        heard = 0  # lines carried by the sound sentence (voice-over)
        for x in cut["lines"]:
            quiet = [n for n in on if n != x["speaker"]]
            if x["where"] == "on screen":
                parts.append(f'{x["speaker"]} says in English: "{x["line"].strip()}"')
                rest = f"; {' and '.join(quiet)} keep quiet, mouths closed" if quiet else ""
                parts.append(f"Only {x['speaker']}'s lips move{rest}.")
            elif on:  # a voice-over: the line is in the sound sentence, the frame listens
                voice.append(f'Off screen, {x["speaker"]}\'s voice: "{x["line"].strip()}"')
                heard += 1
            else:
                parts.append(f'{x["speaker"]} says in an off-screen voice: "{x["line"].strip()}"')
        if heard == len(cut["lines"]) and on:
            parts.append(f"{' and '.join(on)} listen{'s' if len(on) == 1 else ''} in silence, "
                         "mouth closed; no one in frame speaks.")  # fmt: skip
        elif not cut["lines"]:
            parts.append("Everyone stays silent, mouths closed." if on else "The frame is silent.")
        blocks.append(PromptBlock(sub_shot=cut["index"], text=" ".join(parts),
                                  camera=_camera(cut.get("camera", ""))))  # fmt: skip
    sound = context["shot"].get("sound", "").strip().rstrip(".") or "the room tone of the place"
    spoken = any(c["lines"] for c in context["cuts"])
    lead = "Apart from the spoken lines, the only sounds are" if spoken else (
        "Everyone stays silent; the only sounds are")  # fmt: skip
    sound = f"{lead} {sound[0].lower()}{sound[1:]}."
    if voice:
        sound = " ".join([*voice, sound])
    return VideoPromptOutput(blocks=blocks, sound=sound,
                             notes="Offline sample: built from the plan.")  # fmt: skip


PERSON_KINDS = ("face", "look", "chosen")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _low(text: str) -> str:
    """Lowercase the first letter, unless the first word is a name or an acronym."""
    word = text.split(" ", 1)[0]
    return text if word[1:2].isupper() or word in ("I",) else text[:1].lower() + text[1:]


def _camera(text: str) -> str:
    low = text.lower()
    for move in CAMERA_MOVES:
        if move in low or move.replace(" ", "-") in low:
            return move
    if "push" in low or "dolly in" in low:
        return "push in with small amplitude at slow speed"
    return "static shot"


def fixture_video_prompt(request: GenerationRequest) -> VideoPromptOutput:
    """Offline stand-in for the writer: the template."""
    return template_prompt(request.context)
