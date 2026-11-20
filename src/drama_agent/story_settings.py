"""A story's settings that can change after it was made: its rating and its pace.

Each is the value set on the story's Overview (stored per story), else the one its newest
job was made with, else the default.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def story_setting(
    repo: Any,
    package_id: str | None,
    name: str,
    from_options: Callable[[Any], T],
    parse: Callable[[str], T],
    default: T,
) -> T:
    """The setting `name` of a story as it applies now: changed on its Overview (parsed),
    else its newest job's options (`from_options`), else `default` (also for no story)."""
    if not package_id:
        return default
    if changed := repo.get_story_setting(package_id, name):
        return parse(changed)
    jobs = repo.jobs_for_package(package_id)  # newest first
    return from_options(jobs[0].request.options) if jobs else default
