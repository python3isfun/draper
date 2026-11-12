"""Prompt assets. The base orchestrator prompt is spec section 10, in English, with the
story language and setting fixed to American English and the present-day United States."""

from functools import cache
from importlib.resources import files

ORCHESTRATOR_PROMPT_VERSION = "spec-v0.2-s10-en-us"


@cache
def load_orchestrator_prompt(language: str = "en-US") -> str:
    return files(__package__).joinpath(f"orchestrator.{language}.md").read_text(encoding="utf-8")


@cache
def load_h3_image_rules() -> str:
    """MiniMax-H3-Image prompt rules, shared with the minimax-h3-image-prompt skill."""
    return files(__package__).joinpath("h3_image_rules.md").read_text(encoding="utf-8")


@cache
def load_h3_image_examples() -> str:
    return files(__package__).joinpath("h3_image_examples.md").read_text(encoding="utf-8")
