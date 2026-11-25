"""Story templates learned from existing films and dramas (docs/templates.md).

A template is the craft of one title, abstracted: its hook, the beats of its three episodes,
its roles and how they relate, its tropes and reversals, its look, its pace and its music.
A creator picks one for a new story and brings their own premise; the template guides every
step, and the story, its people, places and lines stay the creator's.

What a template never holds is the title's own expression: its characters' and places'
names, its dialogue, its signature scenes. `source_specifics` lists those so that neither
the template nor a story made from it can use them (`copied_specifics`).

Templates are JSON files in `templates/` (DRAMA_TEMPLATES_DIR), meant to be committed. The
material they are learned from (a script in `stories/`) stays local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .llm import GenerationRequest
from .models import Issue
from .models.package import utcnow
from .pace import PACES, Pace

TEMPLATE_RULES_VERSION = "1"
LEARN_PROMPT_VERSION = "1"
TemplateSource = Literal["model_knowledge", "local_script"]
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


class LearnedFrom(BaseModel):
    """The title a template was learned from. For the team: creators see the template's
    own name, and a story made from it doesn't name the title."""

    title: str
    year: int | None = None
    kind: str = "film"  # film, series, short drama
    sources: list[TemplateSource] = Field(default_factory=list)


class RoleArchetype(BaseModel):
    role: str  # "the guarded heir", never the source character's name
    function: str  # what the role does for the story
    arc: str  # how the role changes across the three episodes


class EpisodeBeat(BaseModel):
    episode: int = Field(ge=1, le=3)
    at: str  # where in the 120-second episode: "0-5 s", "midpoint", "last 10 s"
    beat: str  # what happens, as a pattern ("a public humiliation the lead endures")
    purpose: str  # why it is there (hook, escalation, reveal, cliffhanger)


class TemplateLook(BaseModel):
    medium: str  # live-action, the camera and lens feel
    color_and_light: str
    camera_and_framing: str  # shot grammar: what is held close, how reveals are staged
    wardrobe_and_design: str  # how costume and sets carry status, mood and change


class TemplateBody(BaseModel):
    """What the model writes: the craft, abstracted. Every field is a pattern another
    story can fill with its own people, places and events."""

    name: str  # the pattern's own name ("Contract marriage, hidden heir"), not the title
    summary: str  # one line: what a creator gets from this template
    genre: str
    audience: str
    emotional_goal: str
    premise_pattern: str  # the shape of the premise, with no names
    hook: str  # how the first seconds grab the viewer
    roles: list[RoleArchetype]
    relationships: list[str]  # between roles ("the rival knows the lead's secret")
    structure: list[EpisodeBeat]  # the beats of the three 120-second episodes
    cliffhangers: list[str]  # how episodes end
    tropes: list[str]
    reversals: list[str]
    dialogue_style: str  # register and rhythm, never quoted lines
    set_archetypes: list[str]  # kinds of places ("a glass-walled office above the city")
    prop_motifs: list[str]  # objects that carry the story ("a contract signed in haste")
    look: TemplateLook
    pace: Pace
    pacing_notes: str
    music: str  # a caption for the music beds: instruments, mood, where it swells
    # The title's own expression, never to be reused: its characters' and places' names,
    # catchphrases, and signature scenes in a few words each.
    source_specifics: list[str]


class StoryTemplate(TemplateBody):
    template_id: str
    learned_from: LearnedFrom
    version: int = 1
    learn_prompt_version: str = LEARN_PROMPT_VERSION
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------------------
# Files


def templates_dir() -> Path:
    return Path(os.environ.get("DRAMA_TEMPLATES_DIR", "templates"))


def list_templates(root: Path | None = None) -> list[StoryTemplate]:
    """Every readable template, by name. A file that doesn't parse is skipped."""
    root = root or templates_dir()
    out = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            out.append(StoryTemplate.model_validate_json(path.read_text()))
        except (OSError, ValidationError):
            continue
    return sorted(out, key=lambda t: t.name.casefold())


def get_template(template_id: str, root: Path | None = None) -> StoryTemplate | None:
    if not _ID.match(template_id):
        return None
    path = (root or templates_dir()) / f"{template_id}.json"
    try:
        return StoryTemplate.model_validate_json(path.read_text())
    except (OSError, ValidationError):
        return None


def save_template(t: StoryTemplate, root: Path | None = None) -> Path:
    root = root or templates_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{t.template_id}.json"
    path.write_text(t.model_dump_json(indent=2) + "\n")
    return path


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    return s[:48] or "template"


# ---------------------------------------------------------------------------------------
# Using a template in a job


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def template_rules(t: StoryTemplate, stage: str | None) -> str:
    """The template's part of a step's instructions: the whole pattern for the outline, the
    parts each later step needs, and always the originality rule."""
    head = (
        f'Story template "{t.name}" ({t.genre}): {t.summary}\n'
        "It is a pattern of structure, roles and craft learned from an existing work. Use it "
        "as a pattern only. The premise's specifics, every character, name, place, line of "
        "dialogue and scene must be the creator's and original: build them from the sources, "
        "not from the work the template was learned from. Never use, or closely paraphrase, "
        "any of these (that work's own expression):\n" + _lines(t.source_specifics)
    )
    story = (
        f"Premise pattern: {t.premise_pattern}\nHook: {t.hook}\n"
        "Roles (cast the creator's characters into them; one character may carry two):\n"
        + _lines([f"{r.role}: {r.function} Arc: {r.arc}" for r in t.roles])
        + "\nRelationships:\n" + _lines(t.relationships)
        + "\nBeats of the three episodes (fit them to the story; timings are guides):\n"
        + _lines([f"Episode {b.episode}, {b.at}: {b.beat} ({b.purpose})" for b in t.structure])
        + "\nEpisode endings:\n" + _lines(t.cliffhangers)
        + "\nTropes:\n" + _lines(t.tropes)
        + "\nReversals:\n" + _lines(t.reversals)
    )  # fmt: skip
    design = (
        f"Look: {t.look.medium}; {t.look.color_and_light}\n"
        f"Wardrobe and design: {t.look.wardrobe_and_design}\n"
        "Kinds of places:\n" + _lines(t.set_archetypes)
        + "\nObjects that carry the story:\n" + _lines(t.prop_motifs)
    )  # fmt: skip
    screen = (
        f"Look: {t.look.medium}; {t.look.color_and_light}\n"
        f"Camera and framing: {t.look.camera_and_framing}\n"
        f"Pacing: {t.pacing_notes}"
    )
    part = {
        "outline": story,
        "script": f"Hook: {t.hook}\nDialogue: {t.dialogue_style}\nEpisode endings:\n"
        + _lines(t.cliffhangers),
        "cast": design,
        "sets": design,
        "props": design,
        "storyboard": screen + "\nWrite the style brief from this look.",
        "review": "Check that the story follows the template's pattern without retelling "
        "the work it was learned from: different people, places, events and lines.",
    }.get(stage or "", "")
    return head + ("\n\n" + part if part else "")


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.casefold()))


def copied_specifics(t: StoryTemplate, text: str) -> list[str]:
    """The template's source specifics that appear in `text` (whole words, any case)."""
    have = f" {_norm(text)} "
    return [s for s in t.source_specifics if (n := _norm(s)) and f" {n} " in have]


def originality_issues(t: StoryTemplate, object_id: str, text: str) -> list[Issue]:
    return [
        Issue(
            check="template_originality",
            object_id=object_id,
            evidence=f'uses "{s}" from the work the template "{t.name}" was learned from; '
            "replace it with the creator's own",
        )
        for s in copied_specifics(t, text)
    ]


# ---------------------------------------------------------------------------------------
# Learning a template

LEARN_INSTRUCTIONS = """\
You study an existing film or drama and write a story template from it: the craft of the \
work, abstracted, so creators can make new and original vertical short dramas with the same \
power. A story made from the template has three episodes of 120 seconds each, shot 9:16.

Capture what makes the work great and transferable:
- the hook in the first seconds; how each episode ends;
- the beats, mapped onto the three 120-second episodes (compress and reorder the work's \
arc: the opening, the turning points, the midpoint, the crisis and the payoff);
- the roles as archetypes (what each does for the story, and its arc) and how they relate;
- the tropes and reversals, the dialogue's register and rhythm;
- the look: medium, color and light, camera and framing (what is held close, how reveals \
are staged), wardrobe and production design; the pace; the music.

Write patterns, never the work's own expression:
- no character or place names: say "the guarded heir", "a family-run diner";
- no quoted or paraphrased dialogue, no catchphrases;
- no retelling of the work's specific scenes in order: describe each beat as a pattern any \
story could fill ("a public humiliation the lead endures in silence").
Then list in source_specifics the work's own expression a story must not reuse: its \
characters' and places' names, its catchphrases, and its signature scenes in a few words \
each. Only distinctive items: never common words.

name: a name for the pattern itself ("Contract marriage, hidden heir"), not the title. \
pace is one of: brisk, measured, slow_burn. music: one caption for a music generator.
"""


class LearnError(RuntimeError):
    """The model's template kept copying its source."""


def _shingles(text: str, n: int = 8) -> set[str]:
    words = _norm(text).split()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def learn_issues(body: TemplateBody, script: str | None) -> list[Issue]:
    """What a learned template must not do: name its source's specifics in the pattern, or
    copy eight words in a row from the script it was learned from."""
    pattern = body.model_dump(exclude={"source_specifics"})
    text = json.dumps(pattern)
    probe = StoryTemplate(**body.model_dump(), template_id="probe",
                          learned_from=LearnedFrom(title=""))  # fmt: skip
    issues = [
        Issue(check="template_abstract", object_id="template",
              evidence=f'the pattern names "{s}": describe it as a pattern instead')
        for s in copied_specifics(probe, text)
    ]  # fmt: skip
    if script:
        copied = sorted(_shingles(text) & _shingles(script))
        issues += [
            Issue(check="template_abstract", object_id="template",
                  evidence=f'copies the script: "{c}"')
            for c in copied[:5]
        ]  # fmt: skip
    if {b.episode for b in body.structure} != {1, 2, 3}:
        issues.append(Issue(check="template_structure", object_id="template",
                            evidence="structure needs beats for episodes 1, 2 and 3"))  # fmt: skip
    if len(body.source_specifics) < 3:
        issues.append(Issue(check="template_specifics", object_id="template",
                            evidence="list at least three of the work's own names, "
                            "catchphrases or signature scenes"))  # fmt: skip
    return issues


async def learn_template(
    client,
    title: str,
    *,
    year: int | None = None,
    kind: str = "film",
    notes: str = "",
    script: str | None = None,
    template_id: str | None = None,
    attempts: int = 3,
) -> StoryTemplate:
    """Learn a template from a title: from the model's knowledge of it, and from its script
    when one is given (read here, never stored). Raises LearnError when the result still
    copies its source after `attempts` tries."""
    context = {"title": title, "year": year, "kind": kind, "notes": notes,
               "script": script, "format": "3 episodes x 120 s, vertical 9:16",
               "paces": list(PACES)}  # fmt: skip
    issues: list[Issue] = []
    for attempt in range(attempts):
        req = GenerationRequest(stage="template", instructions=LEARN_INSTRUCTIONS,  # type: ignore[arg-type]
                                context=context, feedback=issues, attempt=attempt)  # fmt: skip
        body = await client.generate(req, TemplateBody)
        issues = learn_issues(body, script)
        if not issues:
            sources: list[TemplateSource] = ["model_knowledge"]
            if script:
                sources.append("local_script")
            return StoryTemplate(
                **body.model_dump(),
                template_id=template_id or slug(body.name),
                learned_from=LearnedFrom(title=title, year=year, kind=kind, sources=sources),
            )
    raise LearnError("; ".join(i.evidence for i in issues))


def fixture_template(request: GenerationRequest) -> TemplateBody:
    """Offline sample (the fixture model client)."""
    return TemplateBody(
        name="Secret identity, public fall",
        summary="A lead hiding who they are is humiliated in public, then turns it around.",
        genre="romantic drama",
        audience="adults who like reversals and slow-revealed secrets",
        emotional_goal="the satisfaction of a hidden strength revealed",
        premise_pattern="Someone with a hidden standing endures contempt from people who "
        "don't know who they are, until a moment forces the truth out.",
        hook="Open on the lead being shamed in front of a crowd, one detail hinting at more.",
        roles=[
            RoleArchetype(
                role="the hidden lead",
                function="carries the secret",
                arc="from silence to claiming their place",
            ),
            RoleArchetype(
                role="the scornful rival",
                function="drives the humiliation",
                arc="from triumph to exposure",
            ),
            RoleArchetype(
                role="the one who notices",
                function="sees the lead's worth first",
                arc="from doubt to loyalty",
            ),
        ],
        relationships=["The rival wants what the lead secretly already has."],
        structure=[
            EpisodeBeat(episode=1, at="0-5 s", beat="a public humiliation", purpose="hook"),
            EpisodeBeat(
                episode=1, at="last 10 s", beat="a hint of the secret", purpose="cliffhanger"
            ),
            EpisodeBeat(
                episode=2, at="midpoint", beat="the rival raises the stakes", purpose="escalation"
            ),
            EpisodeBeat(
                episode=3, at="last 20 s", beat="the reveal before everyone", purpose="payoff"
            ),
        ],
        cliffhangers=["End on a look that says the lead knows something nobody else does."],
        tropes=["hidden identity", "public humiliation and reversal"],
        reversals=["The person everyone pitied holds the power."],
        dialogue_style="Short, cutting lines from the rival; the lead answers with few words.",
        set_archetypes=["a crowded formal event", "a modest home with one telling object"],
        prop_motifs=["an object that proves who the lead really is"],
        look=TemplateLook(
            medium="live-action, glossy, shallow depth of field",
            color_and_light="cold light on the rival's world, warm at home",
            camera_and_framing="close on reactions; reveals in slow push-ins",
            wardrobe_and_design="status shown by tailoring and jewelry",
        ),
        pace="brisk",
        pacing_notes="A new humiliation or hint every 15 seconds.",
        music="Tense strings under the scorn, a swelling piano at the reveal.",
        source_specifics=["Sample Heir", "Golden Gala", "Bow before me"],
    )


# ---------------------------------------------------------------------------------------
# Command line: python -m drama_agent.templates learn "Title" [--script stories/...]


def _main(argv: list[str] | None = None) -> int:
    from .config import client_from_env, load_dotenv

    p = argparse.ArgumentParser(prog="python -m drama_agent.templates")
    sub = p.add_subparsers(dest="cmd", required=True)
    learn = sub.add_parser("learn", help="learn a template from a film or drama")
    learn.add_argument("title")
    learn.add_argument("--year", type=int)
    learn.add_argument("--kind", default="film", help="film, series or short drama")
    learn.add_argument("--notes", default="", help="what to pay attention to")
    learn.add_argument("--script", type=Path, help="a local script or synopsis (not stored)")
    learn.add_argument("--id", help="the template's id (default: from its name)")
    sub.add_parser("list", help="list the templates")
    args = p.parse_args(argv)

    if args.cmd == "list":
        for t in list_templates():
            print(f"{t.template_id}\t{t.name}\t(from {t.learned_from.title})")
        return 0
    load_dotenv()
    script = args.script.read_text() if args.script else None
    try:
        t = asyncio.run(learn_template(client_from_env(), args.title, year=args.year,
                                       kind=args.kind, notes=args.notes, script=script,
                                       template_id=args.id))  # fmt: skip
    except LearnError as exc:
        print(f"Not saved: the template still copies its source ({exc}).", file=sys.stderr)
        return 1
    print(save_template(t))
    return 0


if __name__ == "__main__":
    # Run as `python -m`, this file is __main__, a second copy of the module: use the
    # imported one, so the models are the same classes the clients and the API use.
    from drama_agent.templates import _main as main

    raise SystemExit(main())
