"""Casting across stories (phase 3 of docs/entity-reorganization.md): for a story's
characters and sets, the library assets of other stories that may be the same, for a
person to confirm. Matching is by name, deterministic and free: the same name, one name
containing the other, or the same first name, in that order. A person decides; nothing is linked here.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import LibraryAsset, asset_id

_KINDS = ("character", "set")


def normalize(name: str) -> str:
    """A name for comparing: lower case, width-folded (full-width brackets and the like),
    without spaces and punctuation; letters of any script are kept."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _first(name: str) -> str:
    # NFKC folds full-width brackets and commas to ASCII first.
    words = re.split(r"[\s\u00b7,()]+", unicodedata.normalize("NFKC", name).casefold().strip())
    return words[0] if words and words[0] else ""


def score(a: str, b: str) -> tuple[float, str] | None:
    """How alike two names are, and why; None when they aren't."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return None
    if na == nb:
        return 1.0, "same name"
    if min(len(na), len(nb)) >= 3 and (na in nb or nb in na):
        return 0.7, "one name contains the other"
    fa, fb = _first(a), _first(b)
    if fa and fa == fb and len(fa) >= 3:
        return 0.5, "same first name"
    return None


def matches(repo: Any, package_id: str) -> list[dict[str, Any]]:
    """Each of the story's characters and sets with its own library asset, its current link
    (if a person linked it to another story's asset), and candidates from other stories,
    best first."""
    rows = [c for c in repo.list_casting(package_id=package_id) if c.kind in _KINDS]
    titles: dict[str, str] = {}

    def title(pid: str) -> str:
        if pid not in titles:
            pkg = repo.get_package(pid)
            titles[pid] = pkg.story_bible.title if pkg is not None else pid
        return titles[pid]

    others = {
        kind: [a for a in repo.list_library_assets(kind=kind)
               if a.origin_package_id != package_id and a.merged_into is None
               and not a.archived]
        for kind in _KINDS
    }  # fmt: skip
    out = []
    for c in rows:
        own: LibraryAsset | None = repo.get_library_asset(asset_id(c.kind, package_id, c.story_id))
        if own is None:
            continue
        linked = repo.get_library_asset(c.asset_id) if c.source == "user" else None
        found = []
        for a in others[c.kind]:
            if (s := score(own.name, a.name)) is not None:
                found.append({"asset_id": a.asset_id, "name": a.name, "score": s[0],
                              "reason": s[1], "story": title(a.origin_package_id),
                              "package_id": a.origin_package_id})  # fmt: skip
        found.sort(key=lambda x: (-x["score"], x["name"]))
        out.append({
            "story_id": c.story_id, "kind": c.kind, "name": own.name, "own_asset_id": own.asset_id,
            "linked": {"asset_id": linked.asset_id, "name": linked.name,
                       "story": title(linked.origin_package_id)} if linked else None,
            "candidates": found,
        })  # fmt: skip
    return out
