"""Keep human-facing text free of internal IDs, field names and timecodes.

Models sometimes write `成片[0,10000)ms。` timeline prefixes or `prop_rentfile` into action
lines. `normalize_prose` fixes what can be fixed safely and deterministically (no extra
model calls); `find_prose_leaks` reports what is left so the step can be repaired.

Only prose fields are touched: ID fields (`*_id`, `*_ids`, `*_refs`), map keys and
production notes meant for tooling (`generation_notes`) are left alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from ..models import Issue
from ..models.story import StoryBible

# Not prose: tooling notes and closed-vocabulary fields.
SKIP_FIELDS = {"generation_notes", "speech_rate_assumption", "int_ext", "voice_only"}

_SPAN = r"[\[(]\s*\d+\s*[,，]\s*\d+\s*[)\]]\s*ms"
# A timeline span at the very start of a field, e.g. "成片[0,10000)ms。" or "[0, 10000)ms:".
LEADING_SPAN_RE = re.compile(rf"^\s*(?:成片|片段|镜头|shot|Shot)?\s*{_SPAN}\s*[。.:：、,，]?\s*")
SPAN_RE = re.compile(_SPAN)
# A bare duration such as "2300ms" (not part of a span).
DURATION_RE = re.compile(r"(?<![\d.,，\[(])(\d+(?:\.\d+)?)\s*ms(?![A-Za-z])")
# ASCII snake_case tokens: IDs (prop_phone) and field names (spoken_timing_verified).
# The lookbehind is ASCII-only because CJK characters count as \w, so \b misses
# "将prop_rentfile".
SNAKE_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+")
# Episode IDs used as labels ("ep_01：..."), rewritten as 第1集 / Episode 1.
EPISODE_RE = re.compile(r"(?<![A-Za-z0-9_])ep_(\d+)(?![A-Za-z0-9_])")


def entity_names(bible: StoryBible) -> dict[str, str]:
    """Display text for every story-bible ID that can stand in for it in prose."""
    names: dict[str, str] = {}
    names.update({c.character_id: c.name for c in bible.characters})
    names.update({loc.location_id: loc.name for loc in bible.locations})
    names.update({p.prop_id: p.name for p in bible.props})
    names.update({lk.look_id: lk.description for lk in bible.looks})
    names.update({s.location_state_id: s.description for s in bible.location_states})
    names.update({s.prop_state_id: s.description for s in bible.prop_states})
    return names


def _seconds(ms_text: str, language: str) -> str:
    secs = f"{float(ms_text) / 1000:g}"
    return f"{secs}秒" if language.lower().startswith("zh") else f"{secs}s"


def normalize_text(text: str, names: dict[str, str], language: str) -> str:
    out = LEADING_SPAN_RE.sub("", text)
    if names:
        # Longest first so "prop_photo_torn"-style IDs aren't half-replaced by "prop_photo".
        pattern = "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True))
        out = re.sub(
            rf"(?<![A-Za-z0-9_])(?:{pattern})(?![A-Za-z0-9_])",
            lambda m: names[m.group(0)],
            out,
        )
    zh = language.lower().startswith("zh")
    out = EPISODE_RE.sub(
        lambda m: f"第{int(m.group(1))}集" if zh else f"Episode {int(m.group(1))}", out
    )
    return DURATION_RE.sub(lambda m: _seconds(m.group(1), language), out)


def _is_id_field(name: str) -> bool:
    return name.endswith(("_id", "_ids", "_refs")) or name in SKIP_FIELDS


def _owner_id(model: BaseModel, fallback: str) -> str:
    """The stable ID of the object a field belongs to, for issue reporting."""
    for name in type(model).model_fields:
        if name.endswith("_id") and isinstance(getattr(model, name), str):
            return getattr(model, name)
    return fallback


def iter_prose(obj: Any, owner: str = "", path: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield (owner_id, field_path, text) for every prose string under obj."""
    if isinstance(obj, BaseModel):
        owner = _owner_id(obj, owner)
        for name in type(obj).model_fields:
            if _is_id_field(name):
                continue
            yield from iter_prose(getattr(obj, name), owner, f"{path}.{name}" if path else name)
    elif isinstance(obj, str):
        yield owner, path, obj
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from iter_prose(item, owner, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for key, value in obj.items():  # keys are often IDs by design; only values are prose
            yield from iter_prose(value, owner, f"{path}[{key}]")


def normalize_prose(obj: Any, names: dict[str, str], language: str) -> int:
    """Rewrite prose fields in place. Returns how many fields changed."""
    changed = 0
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            if _is_id_field(name):
                continue
            value = getattr(obj, name)
            if isinstance(value, str):
                new = normalize_text(value, names, language)
                if new != value:
                    setattr(obj, name, new)
                    changed += 1
            else:
                changed += normalize_prose(value, names, language)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new = normalize_text(item, names, language)
                if new != item:
                    obj[i] = new
                    changed += 1
            else:
                changed += normalize_prose(item, names, language)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                new = normalize_text(value, names, language)
                if new != value:
                    obj[key] = new
                    changed += 1
            else:
                changed += normalize_prose(value, names, language)
    return changed


def find_prose_leaks(*objs: Any) -> list[Issue]:
    issues: list[Issue] = []
    for obj in objs:
        for owner, path, text in iter_prose(obj):
            found = (
                SNAKE_RE.findall(text)
                + SPAN_RE.findall(text)
                + [m.group(0) for m in DURATION_RE.finditer(text)]
            )
            if found:
                issues.append(
                    Issue(
                        check="prose_leak",
                        object_id=owner or path,
                        evidence=f"{path} contains {sorted(set(found))[:5]}: {text[:80]}",
                        severity="major",
                        fix_scope="Rewrite as plain prose: names instead of IDs, no field "
                        "names, no timecodes (timing belongs in the *_ms fields).",
                    )
                )
    return issues
