"""Takes: explore, develop, a new take, an edited take, more draws, picks and imports."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from ..models import ProductionPackage
from ..models.images import DesignOption, GeneratedImage
from ..models.package import utcnow
from .prompts import DEFAULT_SIZE, NO_OTHER_TEXT, SIZES, _prompt_and_refs, _Slot, _subject, key_of
from .slots import NeedsPick, _graph, selected_image, slot_title


def finalize_prompt(view: str) -> str:
    """Full-size render of a picked draft, as an edit that keeps it (MiniMax's edit form)."""
    what = f"{view} photograph" if view else "photograph"
    return (
        f"The {what} in <Picture 1> is <Subject 1>. The target image is <Subject 1> rendered at "
        "full resolution, with finer natural detail in skin, hair, fabric and surfaces. "
        "<Subject 1> is fully preserved. "
        f"{NO_OTHER_TEXT}"
    )


def _is_draft(size: str) -> bool:
    return size in {small for _, small in SIZES.values()} | {DEFAULT_SIZE[1]}


def _new_take_group(
    pkg: ProductionPackage,
    slot: _Slot,
    slots: list[_Slot],
    *,
    takes: int,
    draws: int,
    draft: bool,
    job_id: str | None,
    first_index: int,
    direction: str | None = None,
    selected_design: dict[str, str] | None = None,
) -> tuple[list[DesignOption], list[GeneratedImage]]:
    """`takes` pending options (written later by the prompt writer, in one go) with
    `draws` pending images each. References to other images are dynamic ("sel:<key>"):
    they resolve to the selected image of that deliverable when the image is generated."""

    def current(key: tuple[str, str]) -> Any:
        return _Ref(f"sel:{key[0]}|{key[1]}")

    refs, template, brief = _prompt_and_refs(pkg, slot, slots, current)
    if selected_design:
        brief["selected_design"] = selected_design
    full, small = SIZES.get(slot.view, DEFAULT_SIZE)
    size = small if draft else full
    group = f"grp_{uuid.uuid4().hex[:10]}"
    options, images = [], []
    for t in range(takes):
        opt = DesignOption(
            option_id=f"opt_{uuid.uuid4().hex[:12]}",
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            requirement_id=slot.req.requirement_id,
            deliverable_id=slot.deliverable_id,
            group_id=group,
            index=first_index + t,
            label=f"Take {first_index + t}",
            prompt=template,
            direction=direction,
            brief=brief,
            size=size,
            job_id=job_id,
        )
        options.append(opt)
        images += draws_for(opt, draws, refs)
    return options, images


@dataclass
class _Ref:
    image_id: str


def prompt_source(option: DesignOption) -> str:
    """The prompt_source of an image drawn from this take."""
    return {"pending": "pending", "written": "model", "edited": "user"}.get(
        option.status, "template"
    )


def draws_for(
    option: DesignOption,
    n: int,
    refs: list[str],
    first: int = 1,
    seeds: list[int] | None = None,
) -> list[GeneratedImage]:
    """`n` draws of a take; `seeds` (if given) are used first, then random ones."""
    seeds = list(seeds or [])[:n]
    seeds += [random.randrange(2**31) for _ in range(n - len(seeds))]
    return [
        GeneratedImage(
            image_id=f"img_{uuid.uuid4().hex[:12]}",
            package_id=option.package_id,
            revision_id=option.revision_id,
            requirement_id=option.requirement_id,
            deliverable_id=option.deliverable_id,
            option_id=option.option_id,
            draw=first + i,
            prompt=option.prompt,
            prompt_source=prompt_source(option),
            prompt_model=option.prompt_model,
            brief=option.brief,
            job_id=option.job_id,
            size=option.size,
            seed=seeds[i],
            reference_ids=list(refs),
        )
        for i in range(n)
    ]


def take_draws(images: list[GeneratedImage], option_id: str) -> list[GeneratedImage]:
    """A take's own draws in draw order (not the full-size renders of picked drafts)."""
    mine = [i for i in images if i.option_id == option_id and not i.finalizes]
    return sorted(mine, key=lambda i: (i.draw, i.created_at))


def edit_take(
    source: DesignOption,
    options: list[DesignOption],
    images: list[GeneratedImage],
    prompt: str,
    *,
    draws: int,
    warnings: list[str] | None = None,
    design: dict[str, Any] | None = None,
) -> tuple[DesignOption, list[GeneratedImage]]:
    """A new take with the user's edited prompt, next to `source` (same group, brief,
    size and references). Its draws reuse the source's seeds in order, so each image
    differs from the source's draw of the same number only by the prompt change. `design`
    (design_text now) replaces the brief's design text: the user writes for the text as
    it is, so the take isn't out of date after a hand edit to the sheets."""
    key = key_of(source)
    index = 1 + max((o.index for o in options if key_of(o) == key), default=0)
    opt = source.model_copy(
        update={
            "option_id": f"opt_{uuid.uuid4().hex[:12]}",
            "index": index,
            "status": "edited",
            "label": f"Edit of take {source.index}",
            "profile": source.profile,
            "prompt": prompt,
            "prompt_model": None,
            "direction": None,
            "edited_from": source.option_id,
            "brief": {**source.brief, **(design or {})},
            "warnings": list(warnings or []),
            "error": None,
            "created_at": utcnow(),
        },
        deep=True,
    )
    src = take_draws(images, source.option_id)
    refs = src[0].reference_ids if src else []
    return opt, draws_for(opt, draws, refs, seeds=[i.seed for i in src])


SCOPES = {
    "cast": ("character_identity", "character_look"),
    "sets": ("location",),
    "props": ("prop",),
}


def explore(
    pkg: ProductionPackage,
    options: list[DesignOption],
    *,
    takes: int,
    draws: int,
    draft: bool,
    scope: list[str],
    job_id: str | None,
) -> tuple[list[DesignOption], list[GeneratedImage]]:
    """New takes x draws for every anchor deliverable in scope (cast/sets/props)."""
    kinds = {k for s in scope for k in SCOPES.get(s, ())}
    g = _graph(pkg)
    new_opts, new_imgs = [], []
    for key in g.anchors:
        slot = g.slot(key)
        if slot is None or slot.req.asset_type not in kinds:
            continue
        first = 1 + max((o.index for o in options if key_of(o) == key), default=0)
        o, i = _new_take_group(
            pkg, slot, g.slots, takes=takes, draws=draws, draft=draft, job_id=job_id,
            first_index=first,
        )  # fmt: skip
        new_opts += o
        new_imgs += i
    return new_opts, new_imgs


def _picked(images: list[GeneratedImage], key: tuple[str, str]) -> bool:
    return any(i.selected and i.status == "done" and key_of(i) == key for i in images)


def new_take(
    pkg: ProductionPackage,
    options: list[DesignOption],
    images: list[GeneratedImage],
    key: tuple[str, str],
    *,
    direction: str | None,
    draws: int,
    draft: bool,
    job_id: str | None,
) -> tuple[list[DesignOption], list[GeneratedImage]]:
    """One more take on a deliverable, optionally steered by the user's direction. A
    dependent needs its parent picked first (NeedsPick), as in develop."""
    g = _graph(pkg)
    slot = g.slot(key)
    if slot is None:
        raise KeyError(f"no deliverable {key[1]} on {key[0]}")
    parent = g.parent[key]
    if parent is not None and not _picked(images, parent):
        raise NeedsPick(parent)
    first = 1 + max((o.index for o in options if key_of(o) == key), default=0)
    return _new_take_group(
        pkg, slot, g.slots, takes=1, draws=draws, draft=draft, job_id=job_id, first_index=first,
        direction=direction, selected_design=_selected_design(options, images, parent),
    )  # fmt: skip


def _selected_design(
    options: list[DesignOption],
    images: list[GeneratedImage],
    parent: tuple[str, str] | None,
) -> dict[str, str] | None:
    """For a dependent: the label and profile of the take picked for its parent."""
    if parent is None:
        return None
    img = selected_image(images, parent)
    opt = next((o for o in options if img and o.option_id == img.option_id), None)
    if opt is None or not (opt.label or opt.profile):
        return None
    return {"label": opt.label, "profile": opt.profile}


def intimate_looks(pkg: ProductionPackage) -> set[tuple[str, str]]:
    """(character_id, look_id) of looks worn only in scenes staged as implied or simulated
    intimacy that the character is part of: costumes that exist for those scenes (the
    morning after, the outfit torn open). A look also worn elsewhere is everyday clothing,
    and so is a kiss-level scene's."""
    intimate = {
        sc.scene_id: set(sc.intimacy.character_ids)
        for ep in pkg.episodes
        for sc in ep.scenes
        if sc.intimacy is not None and sc.intimacy.level in ("implied", "simulated")
    }
    worn: dict[tuple[str, str], bool] = {}  # look -> worn only in intimate scenes so far
    for sh in pkg.shot_list:
        for r in sh.character_look_refs:
            key = (r.character_id, r.look_id)
            here = r.character_id in intimate.get(sh.scene_id, set())
            worn[key] = worn.get(key, True) and here
    return {key for key, only in worn.items() if only}


def develop(
    pkg: ProductionPackage,
    options: list[DesignOption],
    images: list[GeneratedImage],
    *,
    draws: int,
    draft: bool,
    job_id: str | None,
) -> tuple[list[DesignOption], list[GeneratedImage], list[tuple[str, str]], list[str]]:
    """One take with `draws` draws for every dependent deliverable whose parent has a
    selected image and that has no take yet. Returns (options, images, the parents still
    waiting for a pick, notes on deliverables left out).

    A look worn only in intimate scenes (see intimate_looks) is left out when the picked face
    shows a real person's likeness (content rule 3): it is drawn from the face, so it would
    put that person into an intimate scene's costume."""
    g = _graph(pkg)
    anchors = set(g.anchors)
    has_take = {key_of(o) for o in options}
    intimate = intimate_looks(pkg)
    waiting: set[tuple[str, str]] = set()
    notes: list[str] = []
    new_opts, new_imgs = [], []
    for slot in g.slots:
        key = slot.key
        if key in anchors or key in has_take:
            continue
        parent = g.parent[key]
        if parent is not None and not _picked(images, parent):
            waiting.add(parent)
            continue
        face = selected_image(images, parent) if parent is not None else None
        if (
            face is not None
            and face.likeness
            and slot.req.asset_type == "character_look"
            and _subject(slot.req) in intimate
        ):
            notes.append(
                f"{slot_title(pkg, key)} is worn only in intimate scenes, and the picked face "
                "shows a real person's likeness, so it is not drawn from that face. Pick a "
                "face without a likeness to draw it."
            )
            continue
        o, i = _new_take_group(
            pkg, slot, g.slots, takes=1, draws=draws, draft=draft, job_id=job_id, first_index=1,
            selected_design=_selected_design(options, images, parent),
        )  # fmt: skip
        new_opts += o
        new_imgs += i
    return new_opts, new_imgs, sorted(waiting), notes


def import_pick(
    pkg: ProductionPackage,
    key: tuple[str, str],
    *,
    file: str,
    thumb: str | None,
    content_type: str,
    prompt: str,
    likeness: bool,
    imported_from: str,
    job_id: str | None,
) -> GeneratedImage:
    """An existing image (e.g. from the Experiment tab) as the pick for a deliverable: a
    finished, selected image, so looks and other states are developed from it."""
    slot = _graph(pkg).slot(key)
    if slot is None:
        raise KeyError(f"{key[1]} is not a deliverable of {key[0]}")
    return GeneratedImage(
        image_id=f"img_{uuid.uuid4().hex[:12]}",
        package_id=pkg.package_id,
        revision_id=pkg.revision_id,
        requirement_id=key[0],
        deliverable_id=key[1],
        status="done",
        prompt=prompt,
        prompt_source="user",
        brief={"deliverable": slot.view, "imported_from": imported_from},
        job_id=job_id,
        # Full size (not a draft), so picking it never queues a full-size re-render.
        size=SIZES.get(slot.view, DEFAULT_SIZE)[0],
        seed=0,
        model="imported",
        file=file,
        thumb=thumb,
        content_type=content_type,
        likeness=likeness,
        imported_from=imported_from,
        finished_at=utcnow(),
    )


def select(images: list[GeneratedImage], image_id: str) -> tuple[str, GeneratedImage | None]:
    """Pick an image for its deliverable. Returns (the image to mark selected, a full-size
    render to queue or None). Picking a draft whose full-size render is done picks that
    render; one still queued takes over the pick when it finishes; a draft with none, or
    only failed ones, gets a new one."""
    target = next((i for i in images if i.image_id == image_id), None)
    if target is None:
        raise KeyError(image_id)
    if target.status != "done":
        raise ValueError("only a finished image can be selected")
    finals = [i for i in images if i.finalizes == target.image_id and i.status != "failed"]
    done = max((i for i in finals if i.status == "done"), key=lambda i: i.created_at, default=None)
    if done is not None:
        return done.image_id, None
    if finals or not _is_draft(target.size):
        return target.image_id, None
    view = target.brief.get("deliverable", "")
    # An edit of the picked draft, not a re-render of its prompt: at a new size the same
    # prompt and seed draw a different face, which defeats the pick.
    final = target.model_copy(
        update={
            "image_id": f"img_{uuid.uuid4().hex[:12]}",
            "status": "pending",
            "size": SIZES.get(view, DEFAULT_SIZE)[0],
            "finalizes": target.image_id,
            "prompt": finalize_prompt(view),
            "prompt_source": "template",
            "reference_ids": [target.image_id],
            "selected": False,
            "file": None,
            "thumb": None,
            "error": None,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
        }
    )
    return target.image_id, final
