"""Lookups shared by the keyframe modules: names, dialogue, a shot's scene, intimacy, the
keyframe size, and a per-revision cache."""

from __future__ import annotations

import re
from typing import Any

from ..models import ProductionPackage
from ..models.storyboard import Shot

# ---------------------------------------------------------------------------
# Planning (pure: the API saves what these return)


class NotReady(ValueError):
    """The sub-shot can't be drawn yet (a design pick is missing, or the keyframe of the
    sub-shot before it isn't approved)."""


# Per package revision (revisions never change): names and lines of dialogue, which the
# board, the export and every writer and judge call look up shot by shot.
_REVISION_CACHE: dict[tuple[str, str, str], Any] = {}


def _cached(pkg: ProductionPackage, what: str, build: Any) -> Any:
    # A revision changes in place only by hand edits (sheet_edit), each logged: the count
    # of changes keeps a cache from outliving one.
    key = (pkg.package_id, pkg.revision_id, f"{what}#{len(pkg.change_log)}")
    if key not in _REVISION_CACHE:
        if len(_REVISION_CACHE) > 256:
            _REVISION_CACHE.clear()
        _REVISION_CACHE[key] = build()
    return _REVISION_CACHE[key]


def _names(pkg: ProductionPackage) -> dict[str, str]:
    def build() -> dict[str, str]:
        names = {c.character_id: c.name for c in pkg.story_bible.characters}
        names |= {loc.location_id: loc.name for loc in pkg.story_bible.locations}
        names |= {p.prop_id: p.name for p in pkg.story_bible.props}
        return names

    return _cached(pkg, "names", build)


def _dialogue(pkg: ProductionPackage) -> dict[str, Any]:
    """Every line of dialogue of the package, by its id."""
    return _cached(pkg, "dialogue", lambda: {
        bt.dialogue.dialogue_id: bt.dialogue
        for ep in pkg.episodes for sc in ep.scenes for bt in sc.beats if bt.dialogue is not None
    })  # fmt: skip


def shot_of(pkg: ProductionPackage, shot_id: str) -> Shot:
    shot = next((s for s in pkg.shot_list if s.shot_id == shot_id), None)
    if shot is None:
        raise KeyError(f"no shot {shot_id}")
    return shot


def scene_of(pkg: ProductionPackage, shot: Shot) -> Any:
    return next(
        (sc for ep in pkg.episodes for sc in ep.scenes if sc.scene_id == shot.scene_id), None
    )


def intimate_scene(pkg: ProductionPackage, shot: Shot) -> str | None:
    """The scene's intimacy level when it is staged as implied or simulated, else None."""
    sc = scene_of(pkg, shot)
    level = sc.intimacy.level if sc is not None and sc.intimacy is not None else "none"
    return level if level in ("implied", "simulated") else None


# Keyframes are drawn at about 4 MP, both sides multiples of 32: several times the video's
# resolution (512-768 px short edge), and a quarter of H3's 16 MP, so nine of them stay well
# under the 24 MiB a request takes and each draw is quicker.
KEYFRAME_SIZES: dict[str, str] = {
    "9:16": "1536x2720",
    "16:9": "2720x1536",
    "1:1": "2048x2048",
    "3:4": "1728x2304",
    "4:3": "2304x1728",
    "2:3": "1664x2496",
    "3:2": "2496x1664",
    "21:9": "3136x1344",
}


def frame_size(pkg: ProductionPackage) -> str:
    """The keyframe size: the video's aspect ratio at about 4 MP (vertical 9:16 unless the
    brief says else)."""
    m = re.search(r"(\d+)\s*:\s*(\d+)", pkg.asset_requirements.style_brief.aspect_ratio or "")
    return KEYFRAME_SIZES.get(f"{m.group(1)}:{m.group(2)}" if m else "", KEYFRAME_SIZES["9:16"])
