"""On-demand reference images for a package's asset requirements.

plan_images() turns a package revision into one pending GeneratedImage per required
deliverable of its character, look, location and prop requirements (shot composition
references are not generated). ImageWorker then generates them one at a time, the way the
GPU server works, oldest first.

Consistency comes from reference images (the model's edit mode, <Picture N> in prompts):
- a look uses the character's face image, so the same person appears in every look;
- a location's top-down view uses its wide view, and the location's other states use the
  first state's wide view (same room, different time or condition);
- a prop's other states use the first state's detail image;
- uploaded photos linked through a requirement's source_refs are added too.

Prompts are composed by the program from the design sheets, the style brief and the
requirement, so no extra model call is needed.

The code lives in the `design` package now; this module re-exports it
under the old names."""

from .design.prompts import (
    ASPECTS,
    DEFAULT_SIZE,
    GENERATED_TYPES,
    NO_OTHER_TEXT,
    NO_TEXT,
    SIZES,
    _join,
    _prompt_and_refs,
    _seed,
    _Slot,
    _slots,
    _style,
    _subject,
    key_of,
    latest_images,
    plan_images,
)
from .design.setup import (
    NO_LIKENESS_USES,
    _image_kind,
    image_client_from_env,
    keyframe_providers_from_env,
    make_thumb,
    package_has_uploads,
)
from .design.slots import (
    NeedsPick,
    _Graph,
    _graph,
    _names,
    anchor_keys,
    design_slots,
    selected_image,
    slot_title,
)
from .design.staleness import (
    DESIGN_KEYS,
    PICTURE_OUTDATED,
    PICTURE_REPLACED,
    TEXT_CHANGED,
    _sources,
    design_text,
    drawn_from_any,
    is_current,
    outdated_pictures,
    plan_refresh,
)
from .design.takes import (
    SCOPES,
    _is_draft,
    _new_take_group,
    _picked,
    _Ref,
    _selected_design,
    develop,
    draws_for,
    edit_take,
    explore,
    finalize_prompt,
    import_pick,
    intimate_looks,
    new_take,
    prompt_source,
    select,
    take_draws,
)
from .design.worker import (
    STALE_RUNNING_S,
    ImageWorker,
    ReferenceUnavailable,
)
from .models.images import (
    DesignOption,  # passed along
    GeneratedImage,  # passed along
)

__all__ = [
    "ASPECTS",
    "DEFAULT_SIZE",
    "DESIGN_KEYS",
    "GENERATED_TYPES",
    "NO_LIKENESS_USES",
    "NO_OTHER_TEXT",
    "NO_TEXT",
    "PICTURE_OUTDATED",
    "PICTURE_REPLACED",
    "SCOPES",
    "SIZES",
    "STALE_RUNNING_S",
    "TEXT_CHANGED",
    "DesignOption",
    "GeneratedImage",
    "ImageWorker",
    "NeedsPick",
    "ReferenceUnavailable",
    "_Graph",
    "_Ref",
    "_Slot",
    "_graph",
    "_image_kind",
    "_is_draft",
    "_join",
    "_names",
    "_new_take_group",
    "_picked",
    "_prompt_and_refs",
    "_seed",
    "_selected_design",
    "_slots",
    "_sources",
    "_style",
    "_subject",
    "anchor_keys",
    "design_slots",
    "design_text",
    "develop",
    "drawn_from_any",
    "draws_for",
    "edit_take",
    "explore",
    "finalize_prompt",
    "image_client_from_env",
    "import_pick",
    "intimate_looks",
    "is_current",
    "key_of",
    "keyframe_providers_from_env",
    "latest_images",
    "make_thumb",
    "new_take",
    "outdated_pictures",
    "package_has_uploads",
    "plan_images",
    "plan_refresh",
    "prompt_source",
    "select",
    "selected_image",
    "slot_title",
    "take_draws",
]
