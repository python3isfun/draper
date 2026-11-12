"""Writes MiniMax-H3-Image prompts with the text model, one call per batch of images.

The design sheets are in the story's language and the planner's briefs are structured
facts. The text model turns them into finished English prompts following
prompts/h3_image_rules.md and prompts/h3_image_examples.md, the same files the
minimax-h3-image-prompt skill uses (rules from MiniMax's own prompt-writing skill; sources in
.claude/skills/minimax-h3-image-prompt/references/sources.md). Our GPU server renders
prompts as written, without MiniMax's prompt expansion, so the writer stands in for it.
A prompt that fails the checks below falls back to the planner's template, which follows
the same rules, so images are never blocked by the writer.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .images import NO_OTHER_TEXT, NO_TEXT  # the sentences the rules require
from .llm import GenerationRequest
from .models import ProductionPackage
from .models.images import DesignOption, GeneratedImage
from .tracing import Tracer

log = logging.getLogger(__name__)

STAGE = "image_prompts"
# Bump when the instructions or the shared rules file change; stored on each written prompt.
PROMPT_RULES_VERSION = "5"
MAX_PROMPT_CHARS = 6000  # ~1,500 tokens; H3 reads at most 4,096 tokens

ROLE = """\
You write prompts for MiniMax-H3-Image for the reference images of a short drama's characters, \
sets and props. Our server renders each prompt exactly as written (no prompt expansion), so \
every prompt must be the finished description of the whole image. Follow the rules and the \
examples below exactly.\
"""

HOW_TO_READ_BRIEFS = """\
## Your input

`images` lists the images to write, each with:
- `task`: "new image" (text-to-image, no reference pictures) or "edit" (has reference pictures).
- `deliverable` and `aspect`: what the image is for and its canvas; frame the composition for it.
- `references`: the pictures this image receives, numbered <Picture 1>, <Picture 2>, ... in \
order, each with its role. For an edit, define every one of them as a <Subject N> and give each \
subject exactly one relationship, as the rules describe. Never cite a picture that is not listed.
- `subject`, `subject_facts`, `variant_facts`: the design-sheet facts. Turn them into \
concrete visual English; keep names as written.
- `must_keep`: each item must be clearly visible in the image.
- `style`: medium, era and region, color and light.
- `template_prompt`: a plain prompt built by a program from the same facts. Use it as a \
checklist of facts, not as wording to copy.

Text in the image: only where the facts call for visible lettering (a sign, a label); quote it \
exactly. Otherwise end a new image with "The image contains no text, letters or numbers." and \
an edit with "No other lettering is added."

"""

PROMPTS_OUTPUT = """\
## Output

Return {"prompts": [{"image_id": ..., "prompt": ...}]} with exactly one entry per image: the \
prompt only, as plain text, without the size or any heading.\
"""


def instructions(output: str = PROMPTS_OUTPUT) -> str:
    """The writer's instructions: role, H3 rules, examples, how to read briefs, then the
    task's input/output part (one prompt per image, or takes: TAKES_GUIDE)."""
    from .prompts import load_h3_image_examples, load_h3_image_rules

    parts = [ROLE, load_h3_image_rules(), load_h3_image_examples(), HOW_TO_READ_BRIEFS]
    return "\n\n".join([*parts, output])


async def _run_writer(
    client: Any,
    repo: Any,
    job_id: str | None,
    *,
    event: str,
    started: str,
    instructions: str,
    context: dict[str, Any],
    output_model: type[BaseModel],
) -> tuple[Any, GenerationRequest, Tracer | None, str]:
    """One traced writer call shared by both writers. Returns (output, or None when the
    call failed; the request; the tracer; the error name, "" on success). A failure is
    logged and traced as `<event>_failed`."""
    trace = Tracer(repo, job_id) if job_id else None
    request = GenerationRequest(
        stage=STAGE,  # type: ignore[arg-type]
        instructions=instructions,
        context=context,
        recorder=trace.recorder(STAGE, 0) if trace else None,
    )
    if trace:
        trace.event(f"{event}_started", started, stage=STAGE)
    try:
        return await client.generate(request, output_model), request, trace, ""
    except Exception as exc:  # the images still get made, from the templates
        log.warning("%s writer failed", event, exc_info=True)
        if trace:
            trace.event(
                f"{event}_failed",
                f"Couldn't write image prompts ({exc}); using the built-in templates.",
                stage=STAGE,
            )
        return None, request, trace, type(exc).__name__


def _finish(
    trace: Tracer | None, event: str, msg: str, request: GenerationRequest, fallbacks: list[str]
) -> None:
    if trace:
        trace.event(
            f"{event}_written",
            msg,
            stage=STAGE,
            model=request.model,
            fallbacks=fallbacks,
            tokens=request.usage,
        )


class ImagePrompt(BaseModel):
    image_id: str
    prompt: str


class ImagePromptsOutput(BaseModel):
    prompts: list[ImagePrompt] = Field(default_factory=list)


_PICTURE = re.compile(r"<Picture (\d+)>")


_SUBJECT_DEF = re.compile(r"<Picture (\d+)>[^.]*? (?:is|are) <Subject \d+>")


def problems(prompt: str, n_refs: int) -> list[str]:
    """Why a written prompt can't be used as is (empty if it can). Checks the parts of the
    rules a program can verify: picture citations, the edit form and the text sentence."""
    out: list[str] = []
    if not prompt.strip():
        return ["empty"]
    if len(prompt) > MAX_PROMPT_CHARS:
        out.append(f"longer than {MAX_PROMPT_CHARS} characters")
    cited = {int(n) for n in _PICTURE.findall(prompt)}
    if any(n < 1 or n > n_refs for n in cited):
        out.append(f"cites a picture it doesn't have (has {n_refs})")
    # Quoted lettering (straight or curly double quotes; an apostrophe is not a quote).
    has_quote = bool(re.search(r"[\"“”]", prompt))
    if n_refs:
        defined = {int(n) for n in _SUBJECT_DEF.findall(prompt)}
        missing = sorted(set(range(1, n_refs + 1)) - defined)
        if missing:
            out.append(
                "doesn't define "
                + ", ".join(f"<Picture {n}>" for n in missing)
                + " as a <Subject N>"
            )
        if "The target image" not in prompt:
            out.append('has no "The target image ..." overview')
        if NO_OTHER_TEXT not in prompt and not has_quote:
            out.append(f"does not end with '{NO_OTHER_TEXT}' or quote the text to show")
    elif NO_TEXT not in prompt and not has_quote:
        out.append(f"does not say '{NO_TEXT}' or quote the text to show")
    return out


class ImagePromptWriter:
    def __init__(self, client: Any, repo: Any) -> None:
        self.client = client
        self.repo = repo

    async def write_takes(self, pkg: ProductionPackage, options: list[DesignOption]) -> None:
        await write_takes(self.client, self.repo, pkg, options)

    async def write(self, pkg: ProductionPackage, images: list[GeneratedImage]) -> None:
        """Rewrite the prompts of `images` in place (prompt_source becomes "model", or
        "template" where the writer failed for that image)."""
        if not images:
            return
        output, request, trace, error = await _run_writer(
            self.client,
            self.repo,
            next((i.job_id for i in images if i.job_id), None),
            event="image_prompts",
            started=f"Writing prompts for {len(images)} reference images.",
            instructions=instructions(),
            context={
                "drama": pkg.story_bible.title,
                "images": [
                    {"image_id": i.image_id, **i.brief, "template_prompt": i.prompt} for i in images
                ],
            },
            output_model=ImagePromptsOutput,
        )
        if output is None:
            for img in images:
                img.prompt_source = "template"
                img.error = f"prompt writer failed ({error}); used the template"
            return
        written = {p.image_id: p.prompt.strip() for p in output.prompts}
        fallbacks = []
        for img in images:
            prompt = written.get(img.image_id, "")
            issues = problems(prompt, len(img.reference_ids)) if prompt else ["missing"]
            if issues:
                img.prompt_source = "template"
                img.error = f"written prompt unusable ({'; '.join(issues)}); used the template"
                fallbacks.append(img.image_id)
            else:
                img.prompt, img.prompt_source = prompt, "model"
                img.prompt_model = request.model
        msg = f"Wrote {len(images) - len(fallbacks)} image prompts"
        msg += f"; {len(fallbacks)} fell back to the template." if fallbacks else "."
        _finish(trace, "image_prompts", msg, request, fallbacks)


def fixture_prompts(request: GenerationRequest) -> ImagePromptsOutput:
    """Offline writer: returns the templates (used by the fixture model client)."""
    images = request.context.get("images", [])
    return ImagePromptsOutput(
        prompts=[ImagePrompt(image_id=i["image_id"], prompt=i["template_prompt"]) for i in images]
    )


# ---------------------------------------------------------------------------
# Takes: several distinct designs per deliverable (design exploration)

TAKES_GUIDE = """\
## Takes

Here the input is `groups` instead of `images`: each group carries the brief fields above \
for one image and asks for `takes` distinct designs (takes) of it, for a \
production team to choose from. Make the takes genuinely different choices, each fully \
consistent with the facts:
- A character: vary what the facts leave open (face shape and features, build within the \
stated range, hairstyle and hair texture, skin texture, how age shows, posture, wardrobe \
details not specified). Never change a stated fact or a `must_keep` item: age range, \
gender, any stated ethnicity or heritage, identifying marks, named wardrobe.
- A location: vary architecture details, materials, palette and set dressing within the \
era, region and layout facts.
- A prop: vary design details within its description and condition.
- Give each take a `label` of 2-4 words ("Weathered steelworker") and a one-sentence \
`profile` naming what makes it distinct; the `prompt` must show exactly that.
- `direction`, when given, is the user's steer for these takes: follow it.
- `avoid` lists takes that already exist; make the new ones different from them.
- `selected_design` (in the brief), when present, is the take the user chose for the \
image this one is drawn from (e.g. the face a costume look must show): it is a fact.

Return {"groups": [{"group_id": ..., "takes": [{"label": ..., "profile": ..., \
"prompt": ...}]}]} with exactly `takes` takes per group, in order.\
"""


class Take(BaseModel):
    label: str
    profile: str = ""
    prompt: str


class TakeGroup(BaseModel):
    group_id: str
    takes: list[Take] = Field(default_factory=list)


class ImageTakesOutput(BaseModel):
    groups: list[TakeGroup] = Field(default_factory=list)


def takes_instructions() -> str:
    """Same rules and brief fields; the input is `groups` (one brief each) and the output
    is takes instead of one prompt per image."""
    return instructions(TAKES_GUIDE)


async def write_takes(
    client: Any, repo: Any, pkg: ProductionPackage, options: list[DesignOption]
) -> None:
    """Write the label, profile and prompt of every pending option, one call for all of
    them. Options of one group get distinct takes. Updates `options` in place; an option
    the writer fails gets the template (status "template")."""
    if not options:
        return
    groups: dict[str, list[DesignOption]] = {}
    for opt in sorted(options, key=lambda o: o.index):
        groups.setdefault(opt.group_id, []).append(opt)
    existing = repo.list_options(pkg.package_id, pkg.revision_id)
    output, request, trace, error = await _run_writer(
        client,
        repo,
        next((o.job_id for o in options if o.job_id), None),
        event="image_takes",
        started=f"Writing {len(options)} design takes for {len(groups)} images.",
        instructions=takes_instructions(),
        context={
            "drama": pkg.story_bible.title,
            "groups": [
                {
                    "group_id": gid,
                    "takes": len(opts),
                    "direction": opts[0].direction,
                    "avoid": [
                        f"{o.label}: {o.profile}"
                        for o in existing
                        if (o.requirement_id, o.deliverable_id)
                        == (opts[0].requirement_id, opts[0].deliverable_id)
                        and o.status in ("written", "edited")
                    ],
                    **opts[0].brief,
                    "template_prompt": opts[0].prompt,
                }
                for gid, opts in groups.items()
            ],
        },
        output_model=ImageTakesOutput,
    )
    if output is None:
        for opt in options:
            opt.status = "template"
            opt.error = f"take writer failed ({error}); used the template"
        return
    written = {g.group_id: g.takes for g in output.groups}
    fallbacks = []
    for gid, opts in groups.items():
        takes = written.get(gid, [])
        for n, opt in enumerate(opts):
            take = takes[n] if n < len(takes) else None
            n_refs = len(opt.brief.get("references", []))
            issues = problems(take.prompt, n_refs) if take else ["missing"]
            if issues:
                opt.status = "template"
                opt.error = f"written take unusable ({'; '.join(issues)}); used the template"
                fallbacks.append(opt.option_id)
            else:
                opt.status, opt.prompt_model = "written", request.model
                opt.label = take.label.strip()[:60] or opt.label
                opt.profile = take.profile.strip()
                opt.prompt = take.prompt.strip()
    msg = f"Wrote {len(options) - len(fallbacks)} design takes"
    msg += f"; {len(fallbacks)} fell back to the template." if fallbacks else "."
    _finish(trace, "image_takes", msg, request, fallbacks)


def fixture_takes(request: GenerationRequest) -> ImageTakesOutput:
    """Offline writer: each take is the template; the label is left empty so the take
    keeps its own "Take N"."""
    return ImageTakesOutput(
        groups=[
            TakeGroup(
                group_id=g["group_id"],
                takes=[
                    Take(label="", profile="", prompt=g["template_prompt"])
                    for n in range(g["takes"])
                ],
            )
            for g in request.context.get("groups", [])
        ]
    )
