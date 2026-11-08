"""Prompt assets. The base orchestrator prompt is taken verbatim from spec section 10."""

from functools import cache
from importlib.resources import files

ORCHESTRATOR_PROMPT_VERSION = "spec-v0.2-s10"


@cache
def load_orchestrator_prompt(language: str = "zh-CN") -> str:
    return files(__package__).joinpath(f"orchestrator.{language}.md").read_text(encoding="utf-8")
