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
from .models.images import GeneratedImage
from .tracing import Tracer

log = logging.getLogger(__name__)

STAGE = "image_prompts"
# Bump when the instructions or the shared rules file change; stored on each written prompt.
PROMPT_RULES_VERSION = "4"
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

## Output

Return {"prompts": [{"image_id": ..., "prompt": ...}]} with exactly one entry per image: the \
prompt only, as plain text, without the size or any heading.\
"""


def instructions() -> str:
    from .prompts import load_h3_image_examples, load_h3_image_rules

    return (
        f"{ROLE}\n\n{load_h3_image_rules()}\n\n{load_h3_image_examples()}\n\n{HOW_TO_READ_BRIEFS}"
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

    async def write(self, pkg: ProductionPackage, images: list[GeneratedImage]) -> None:
        """Rewrite the prompts of `images` in place (prompt_source becomes "model", or
        "template" where the writer failed for that image)."""
        if not images:
            return
        job_id = next((i.job_id for i in images if i.job_id), None)
        trace = Tracer(self.repo, job_id) if job_id else None
        request = GenerationRequest(
            stage=STAGE,  # type: ignore[arg-type]
            instructions=instructions(),
            context={
                "drama": pkg.story_bible.title,
                "images": [
                    {"image_id": i.image_id, **i.brief, "template_prompt": i.prompt} for i in images
                ],
            },
            recorder=trace.recorder(STAGE, 0) if trace else None,
        )
        if trace:
            trace.event(
                "image_prompts_started",
                f"Writing prompts for {len(images)} reference images.",
                stage=STAGE,
            )
        try:
            output = await self.client.generate(request, ImagePromptsOutput)
        except Exception as exc:  # images still get made, from the templates
            log.warning("image prompt writer failed", exc_info=True)
            for img in images:
                img.prompt_source = "template"
                img.error = f"prompt writer failed ({type(exc).__name__}); used the template"
            if trace:
                trace.event(
                    "image_prompts_failed",
                    f"Couldn't write image prompts ({exc}); using the built-in templates.",
                    stage=STAGE,
                )
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
        if trace:
            msg = f"Wrote {len(images) - len(fallbacks)} image prompts"
            msg += f"; {len(fallbacks)} fell back to the template." if fallbacks else "."
            trace.event(
                "image_prompts_written",
                msg,
                stage=STAGE,
                model=request.model,
                fallbacks=fallbacks,
                tokens=request.usage,
            )


def fixture_prompts(request: GenerationRequest) -> ImagePromptsOutput:
    """Offline writer: returns the templates (used by the fixture model client)."""
    images = request.context.get("images", [])
    return ImagePromptsOutput(
        prompts=[ImagePrompt(image_id=i["image_id"], prompt=i["template_prompt"]) for i in images]
    )
