"""Planning design pictures: sizes, slots, each picture's prompt, references and brief."""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from typing import Any

from ..imagegen import MAX_REFERENCES
from ..models import AssetRequirement, ProductionPackage
from ..models.design_sheets import (
    CastLook,
    CastMember,
    PropItem,
    PropVariant,
    SetLocation,
    SetState,
)
from ..models.images import GeneratedImage
from ..pipeline.asset_aggregation import ReuseUnit

GENERATED_TYPES = ("character_identity", "character_look", "location", "prop")

# Deliverable view -> (full size, draft size). Full size is about 4 MP (both sides multiples
# of 32), as for keyframes: H3 is tuned for ~16 MP, but 4 MP is several times what the video
# needs, draws faster, and keeps nine reference pictures well under 24 MiB a request.
# Drafts are about 1 MP.
SIZES: dict[str, tuple[str, str]] = {
    "front face": ("1728x2304", "768x1024"),
    "full body": ("1536x2720", "576x1024"),
    "wide": ("2304x1728", "1024x768"),
    "top-down": ("2048x2048", "1024x1024"),
    "detail": ("2048x2048", "1024x1024"),
}

DEFAULT_SIZE = ("2048x2048", "1024x1024")

ASPECTS = {
    "front face": "3:4 portrait",
    "full body": "9:16 tall portrait",
    "wide": "4:3 landscape",
    "top-down": "1:1 square",
    "detail": "1:1 square",
}


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:4], "big") >> 1


def _join(*parts: str) -> str:
    return "; ".join(p.strip() for p in parts if p and p.strip())


def _style(pkg: ProductionPackage) -> dict[str, str]:
    sb = pkg.asset_requirements.style_brief
    return {"medium": sb.medium, "era_region": sb.era_region, "color_and_light": sb.color_and_light}


# ---------------------------------------------------------------------------
# Planning


@dataclass
class _Slot:
    """One image to make: a deliverable of a requirement."""

    req: AssetRequirement
    deliverable_id: str
    view: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.req.requirement_id, self.deliverable_id)


def _slots(pkg: ProductionPackage) -> list[_Slot]:
    """Images in generation order: identities before looks (looks reference the face),
    then locations and props."""
    order = {t: i for i, t in enumerate(GENERATED_TYPES)}
    reqs = sorted(
        (r for r in pkg.asset_requirements.items if r.asset_type in GENERATED_TYPES),
        key=lambda r: order[r.asset_type],
    )
    return [_Slot(r, d.deliverable_id, d.view) for r in reqs for d in r.deliverables if d.required]


def latest_images(images: list[GeneratedImage]) -> dict[tuple[str, str], GeneratedImage]:
    """The image that stands for each deliverable: the selected finished one, else the
    newest one that hasn't failed, else the newest. (With design takes a deliverable has
    many images; the pick is the one other images are drawn from.)"""

    def rank(img: GeneratedImage) -> tuple[bool, bool, Any, int]:
        return (img.selected and img.status == "done", img.status != "failed",
                img.created_at, img.version)  # fmt: skip

    out: dict[tuple[str, str], GeneratedImage] = {}
    for img in images:
        key = key_of(img)
        if key not in out or rank(img) > rank(out[key]):
            out[key] = img
    return out


def plan_images(
    pkg: ProductionPackage,
    existing: list[GeneratedImage],
    *,
    draft: bool = False,
    only: tuple[str, str] | None = None,
    job_id: str | None = None,
    rewrite: bool = False,
) -> list[GeneratedImage]:
    """New pending images for the deliverables that have none, or whose latest attempt
    failed. With `only` (requirement_id, deliverable_id), a new version of that one image
    (regenerate), with a fresh seed; it keeps the model's written prompt unless `rewrite`
    (its design text changed)."""
    latest = latest_images(existing)
    slots = _slots(pkg)
    # Planned in order, so references can point at images planned earlier in this call.
    planned: dict[tuple[str, str], GeneratedImage] = {}

    def current(key: tuple[str, str]) -> GeneratedImage | None:
        return planned.get(key) or latest.get(key)

    out: list[GeneratedImage] = []
    for slot in slots:
        key = slot.key
        prev = latest.get(key)
        if only is not None:
            if key != only:
                continue
        elif prev is not None and prev.status != "failed":
            continue
        version = 1 + max((i.version for i in existing if key_of(i) == key), default=0)
        seed = random.randrange(2**31) if only else _seed(*key, str(version))
        refs, prompt, brief = _prompt_and_refs(pkg, slot, slots, current)
        source = "pending"
        if only is not None and prev is not None and prev.prompt_source == "model" and not rewrite:
            # Regenerate: same written prompt (same reference layout), new seed.
            prompt, source = prev.prompt, "model"
        full, small = SIZES.get(slot.view, DEFAULT_SIZE)
        img = GeneratedImage(
            image_id=f"img_{uuid.uuid4().hex[:12]}",
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            requirement_id=slot.req.requirement_id,
            deliverable_id=slot.deliverable_id,
            version=version,
            prompt=prompt,
            prompt_source=source,
            brief=brief,
            job_id=job_id,
            size=small if draft else full,
            seed=seed,
            reference_ids=refs,
        )
        planned[key] = img
        out.append(img)
    if only is not None and not out:
        raise KeyError(f"no generated deliverable {only[1]} on {only[0]}")
    return out


def _subject(req: AssetRequirement) -> tuple[str, str | None]:
    ref = req.subject_refs[0]
    return ref.entity_id, ref.variant_id


def _prompt_and_refs(
    pkg: ProductionPackage, slot: _Slot, slots: list[_Slot], current
) -> tuple[list[str], str, dict[str, Any]]:
    """(reference ids, template prompt, brief).

    The brief is what the prompt writer (a text model) turns into the final prompt. The
    template is a usable prompt on its own (offline mode, or when the writer fails), built
    to the same rules (prompts/h3_image_rules.md): say what the image is, then layout,
    visible details and light; end a new image with the no-text sentence. An image with
    reference pictures is an edit in MiniMax's form: each picture is defined as a
    <Subject N>, "The target image ..." states the result, each subject gets one
    relationship (here: partially preserved, or a weak style reference), then what changes,
    ending with "No other lettering is added."
    """
    req = slot.req
    entity_id, variant_id = _subject(req)
    entries: dict[str, Any] = {}
    for sheet in (pkg.cast_sheet, pkg.set_sheet, pkg.prop_sheet):
        if sheet is not None:
            entries |= sheet.entries()
    names = {c.character_id: c.name for c in pkg.story_bible.characters}
    names |= {loc.location_id: loc.name for loc in pkg.story_bible.locations}
    names |= {p.prop_id: p.name for p in pkg.story_bible.props}
    name = names.get(entity_id, entity_id)
    refs: list[str] = []
    roles: list[dict[str, Any]] = []
    definitions: list[str] = []  # "<scope> in <Picture N> is <Subject N>."
    relationships: list[str] = []

    def ref_to(requirement_id: str, suffix: str, scope: str, role: str) -> int | None:
        """Add another image of this package as a reference picture and define its subject;
        returns its number (picture N is subject N)."""
        for s in slots:
            if s.req.requirement_id == requirement_id and s.deliverable_id.endswith(suffix):
                img = current((s.req.requirement_id, s.deliverable_id))
                if img is not None:
                    refs.append(img.image_id)
                    n = len(refs)
                    roles.append({"picture": n, "subject": f"<Subject {n}>", "role": role})
                    definitions.append(f"{scope} in <Picture {n}> is <Subject {n}>.")
                    return n
        return None

    def facts(obj: Any) -> dict[str, Any]:
        if obj is None:
            return {}
        data = obj.model_dump(exclude={"must_keep", "design_freedom", "name"})
        return {k: v for k, v in data.items() if v not in ("", [], None) and not k.endswith("_id")}

    keep = list(req.must_keep)
    subject_facts: dict[str, Any] = {}
    variant_facts: dict[str, Any] = {}
    what = ""  # "A ... photograph of ...": what the image is
    details: list[str] = []
    match req.asset_type:
        case "character_identity":
            m = entries.get(entity_id)
            subject_facts = facts(m)
            what = (
                f"A studio casting photograph of {name}, a close head-and-shoulders portrait, "
                "front-facing with a neutral expression and eyes to the camera, the face filling "
                "most of the frame."
            )
            if isinstance(m, CastMember):
                details.append(
                    _join(m.age_range, m.gender_presentation, f"face: {m.face}", f"hair: {m.hair}")
                    + "."
                )
                details.append(f"Wearing {m.default_wardrobe}, in {m.wardrobe_palette}.")
            else:
                details.append(req.design_brief)
            details.append(
                "A plain light grey seamless backdrop; soft light from the front left and gentle "
                "fill from the right."
            )
        case "character_look":
            m = entries.get(entity_id)
            e = entries.get(variant_id or "")
            subject_facts, variant_facts = facts(m), facts(e)
            n = ref_to(
                ReuseUnit("character_identity", "character", entity_id, None).requirement_id,
                "_face", f"The person ({name})",
                "identity: face, age, build and identifying marks stay",
            )  # fmt: skip
            what = (
                f"A full-length costume photograph of {name}, standing front-facing in a "
                "relaxed neutral pose, head to shoes in frame."
            )
            features = m.distinguishing_features if isinstance(m, CastMember) else ""
            if n:
                relationships.append(
                    f"<Subject {n}> is partially preserved; the face, age and build"
                    + (f" and {features}" if features else "")
                    + " remain, while the clothing, hair, condition and pose change."
                )
            elif features:
                details.append(f"Identifying features: {features}.")
            if isinstance(e, CastLook):
                details.append(_join(f"Wardrobe: {e.wardrobe}", f"hair and makeup: {e.hair_and_makeup}",
                                     e.condition) + ".")  # fmt: skip
            else:
                details.append(req.design_brief)
            details.append("A plain light grey seamless backdrop; soft, even light from the front.")
        case "location":
            base, st = entries.get(entity_id), entries.get(variant_id or "")
            subject_facts, variant_facts = facts(base), facts(st)
            first = next(
                (s for s in slots if s.req.asset_type == "location"
                 and _subject(s.req)[0] == entity_id and s.deliverable_id.endswith("_wide")),
                None,
            )  # fmt: skip
            if slot.deliverable_id.endswith("_layout"):
                # The plan view of a state references that state's wide view (same room,
                # same dressing), seen from above.
                n = ref_to(req.requirement_id, "_wide", f"The set of {name}",
                           "the same set; same inventory and coordinate frame")  # fmt: skip
                what = (
                    f"An overhead plan-view photograph of the film set of {name}, seen from "
                    "directly above with the ceiling removed."
                )
                if n:
                    relationships.append(
                        f"<Subject {n}> is partially preserved; its room shape, furniture, "
                        "openings, positions and materials remain in the same coordinate frame, "
                        "while the viewpoint changes to straight down."
                    )
                details.append(
                    "Every wall opening, door, window and piece of furniture is visible in place, "
                    "with the open floor between them; flat, even light from above."
                )
            elif first is not None and first.req.requirement_id != req.requirement_id:
                n = ref_to(first.req.requirement_id, "_wide", f"The set of {name}",
                           "the same set in another state")  # fmt: skip
                change = _join(
                    *(
                        [st.time_of_day, st.lighting, st.dressing_changes, st.condition,
                         st.changes_from_base]
                        if isinstance(st, SetState) else [req.design_brief]
                    )
                )  # fmt: skip
                what = (
                    f"A production design photograph of the empty set of {name}, from the same "
                    "eye-level camera position."
                )
                if n:
                    relationships.append(
                        f"<Subject {n}> is partially preserved; its layout, furniture, materials "
                        "and camera position remain, while the time of day, light, dressing and "
                        "condition change."
                    )
                details.append(f"Now: {change}.")
            else:
                what = (
                    f"A production design photograph of the empty set of {name}, a wide eye-"
                    "level view of the whole space."
                )
                if isinstance(base, SetLocation):
                    details.append(
                        _join(
                            base.interior_exterior,
                            base.era_and_region,
                            base.layout,
                            base.key_furnishings,
                            base.materials_and_textures,
                            f"palette: {base.color_palette}",
                            f"entrances: {base.entrances_and_blocking}",
                        )
                        + "."
                    )
                else:
                    details.append(req.design_brief)
                if isinstance(st, SetState) and st.lighting:
                    details.append(f"Light: {st.lighting}.")
        case "prop":
            base, st = entries.get(entity_id), entries.get(variant_id or "")
            subject_facts, variant_facts = facts(base), facts(st)
            first = next(
                (s for s in slots if s.req.asset_type == "prop"
                 and _subject(s.req)[0] == entity_id),
                None,
            )  # fmt: skip
            what = (
                f"A product photograph of {name}, a close-up on a plain neutral grey surface "
                "with the object filling most of the frame."
            )
            if first is not None and first.req.requirement_id != req.requirement_id:
                n = ref_to(first.req.requirement_id, "_detail", f"The {name}",
                           "the same object in another state")  # fmt: skip
                change = _join(
                    *(
                        [st.condition, st.changes_from_base]
                        if isinstance(st, PropVariant) else [req.design_brief]
                    )
                )  # fmt: skip
                if n:
                    relationships.append(
                        f"<Subject {n}> is partially preserved; its design, materials, framing, "
                        "camera angle and lighting remain, while its condition changes."
                    )
                details.append(f"Now: {change}.")
            else:
                if isinstance(base, PropItem):
                    details.append(
                        _join(
                            base.size_and_scale,
                            base.materials,
                            base.color_and_finish,
                            base.condition_and_wear,
                            base.distinguishing_marks,
                        )
                        + "."
                    )
                else:
                    details.append(req.design_brief)
                if isinstance(st, PropVariant):
                    details.append(f"Condition: {_join(st.condition)}.")
                details.append("Soft light from the upper left, with gentle fill from the right.")
    # Uploaded photos the requirement cites (e.g. a character reference photo).
    person = req.asset_type in ("character_identity", "character_look")
    for src in req.source_refs:
        if len(refs) >= MAX_REFERENCES:
            break
        refs.append(src.asset_id)
        n = len(refs)
        roles.append(
            {"picture": n, "subject": f"<Subject {n}>", "role": "uploaded reference photo"}
        )
        if person:
            definitions.append(f"The person in <Picture {n}> is <Subject {n}>.")
            relationships.append(
                f"<Subject {n}> is partially preserved; the facial features and build remain, "
                "while the clothing, pose, backdrop and light follow this description."
            )
        else:
            definitions.append(f"The reference photo <Picture {n}> is <Subject {n}>.")
            relationships.append(
                f"<Subject {n}> serves as a weak style reference for {name}, with freely "
                "adapted forms."
            )
    if keep:
        details.append(f"Clearly visible: {_join(*keep)}.")
    sb = _style(pkg)
    details.append(f"Style: {_join(sb['medium'], sb['era_region'], sb['color_and_light'])}.")
    if refs:
        # Edit form: definitions, target overview, relationships, then the changes.
        target = "The target image is " + what[0].lower() + what[1:]
        parts = [*definitions, target, *relationships, *details, NO_OTHER_TEXT]
        task = "edit"
    else:
        parts = [what, *details, NO_TEXT]
        task = "new image"
    brief = {
        "deliverable": slot.view,
        "aspect": ASPECTS.get(slot.view, "1:1 square"),
        "task": task,
        "subject": name,
        "subject_facts": subject_facts,
        "variant_facts": variant_facts,
        "references": roles,
        "must_keep": keep,
        "style": sb,
        "requirement_brief": req.design_brief,
    }
    return refs, " ".join(p.strip() for p in parts if p and p.strip()), brief


NO_TEXT = "The image contains no text, letters or numbers."

NO_OTHER_TEXT = "No other lettering is added."

# ---------------------------------------------------------------------------
# Design exploration: takes (options) x draws, selection, develop


def key_of(x: Any) -> tuple[str, str]:
    """(requirement_id, deliverable_id) of an image, a take or a design slot."""
    if isinstance(x, _Slot):
        return x.key
    return (x.requirement_id, x.deliverable_id)
