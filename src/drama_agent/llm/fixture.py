"""Offline model client that returns the built-in sample package.

Lets the pipeline, API and tests run end to end without a model vendor. `faults` injects
broken outputs for chosen stages to exercise retry and needs_revision paths.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..fixtures import sample
from .base import GenerationRequest, T

# fault(output, attempt) -> output; applied before returning.
Fault = Callable[[Any, int], Any]


class FixtureModelClient:
    model_id = "fixture-v1"

    def __init__(self, faults: dict[str, Fault] | None = None) -> None:
        self.faults = faults or {}
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest, output_model: type[T]) -> T:
        # Imported lazily to avoid a cycle: pipeline.steps imports llm types.
        from ..pipeline.steps import CreativeReviewOutput, ScriptOutput, StoryboardOutput

        self.calls.append(request)
        output: BaseModel
        match request.stage:
            case "ingest":
                output = sample.sample_source_brief()
            case "outline":
                output = sample.sample_story_bible()
            case "script":
                output = ScriptOutput(episodes=sample.sample_episodes())
            case "storyboard":
                shots, timing = sample.sample_storyboard()
                output = StoryboardOutput(
                    shots=shots,
                    timing=timing,
                    style_brief=sample.sample_style_brief(),
                    design_overrides=list(sample.sample_design_overrides().values()),
                )
            case "review":
                output = CreativeReviewOutput()
            case _:
                raise ValueError(f"unknown stage {request.stage}")

        if fault := self.faults.get(request.stage):
            output = fault(output, request.attempt)
        assert isinstance(output, output_model)
        return output
