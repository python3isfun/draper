"""Offline model client that returns the built-in sample package.

Lets the pipeline, API and tests run end to end without a model vendor. `faults` injects
broken outputs for chosen stages to exercise retry and needs_revision paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..fixtures import sample
from ..models import JobRequest
from ..tracing import NULL_RECORDER
from .base import GenerationRequest, T

# fault(output, attempt) -> output; applied before returning.
Fault = Callable[[Any, int], Any]


class FixtureModelClient:
    model_id = "fixture-v1"

    def __init__(self, faults: dict[str, Fault] | None = None, stream_delay_s: float = 0.0) -> None:
        self.faults = faults or {}
        self.calls: list[GenerationRequest] = []
        # >0 streams the output in chunks with pauses, to exercise the live UI offline.
        self.stream_delay_s = stream_delay_s

    async def generate(self, request: GenerationRequest, output_model: type[T]) -> T:
        # Imported lazily to avoid a cycle: pipeline.steps imports llm types.
        from ..pipeline.steps import (
            CastOutput,
            CreativeReviewOutput,
            OutlineOutput,
            PropsOutput,
            ScriptOutput,
            SetsOutput,
            StoryboardOutput,
        )

        self.calls.append(request)
        request.model = self.model_id
        output: BaseModel
        match request.stage:
            case "ingest":
                output = self._source_brief(request)
            case "outline":
                output = OutlineOutput(story_bible=sample.sample_story_bible())
            case "script":
                output = ScriptOutput(episodes=sample.sample_episodes())
            case "cast":
                output = CastOutput(cast_sheet=sample.sample_cast_sheet())
            case "sets":
                output = SetsOutput(set_sheet=sample.sample_set_sheet())
            case "props":
                output = PropsOutput(prop_sheet=sample.sample_prop_sheet())
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
            case "image_prompts":
                from ..image_prompts import ImageTakesOutput, fixture_prompts, fixture_takes

                output = (
                    fixture_takes(request)
                    if output_model is ImageTakesOutput
                    else fixture_prompts(request)
                )
            case _:
                raise ValueError(f"unknown stage {request.stage}")

        rec = request.recorder or NULL_RECORDER
        rec.start(model=self.model_id, api="fixture", request={"stage": request.stage}, turn=0)
        try:
            if fault := self.faults.get(request.stage):
                output = fault(output, request.attempt)
            assert isinstance(output, output_model)
        except BaseException as exc:
            rec.finish(error=f"{type(exc).__name__}: {exc}")
            raise
        rec.delta("commentary", f"Offline sample: returning the built-in {request.stage} output.")
        if request.media:
            rec.delta("commentary", f" ({len(request.media)} attached images were not looked at.)")
        text = output.model_dump_json(indent=1)
        chunks = 12 if self.stream_delay_s else 1
        size = max(1, len(text) // chunks + 1)
        try:
            for i in range(0, len(text), size):
                rec.delta("output", text[i : i + size])
                if self.stream_delay_s:
                    await asyncio.sleep(self.stream_delay_s / chunks)
        except asyncio.CancelledError:
            rec.finish(error="cancelled before the response finished")
            raise
        rec.finish(usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0})
        return output

    @staticmethod
    def _source_brief(request: GenerationRequest) -> BaseModel:
        """The sample brief, with one honest coverage record per source of this job."""
        raw = request.context.get("request")
        job_request = JobRequest.model_validate(raw) if raw else None
        brief = sample.sample_source_brief(job_request)
        if job_request:
            for cov, src in zip(brief.source_coverage, job_request.sources, strict=True):
                if src.type in ("image", "video"):
                    cov.asset_id = src.asset_id
                    cov.analyzed_sections = []
                    cov.notes = "offline sample: the media was not analyzed"
        return brief
