"""The five stateful pipeline steps (spec section 4).

Each step declares its model output type, how to merge that output into the pipeline state,
and the deterministic gate that must pass before the next step runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..models import (
    AssetRequirement,
    AssetRequirements,
    Episode,
    EpisodeTiming,
    Experiment,
    Issue,
    JobRequest,
    ProductionPackage,
    Provenance,
    QAReport,
    Shot,
    SourceBrief,
    StoryBible,
    StyleBrief,
)
from ..models.package import StageName
from ..prompts import ORCHESTRATOR_PROMPT_VERSION
from ..validation import PackageIndex
from ..validation import hard_checks as hc
from .asset_aggregation import aggregate_asset_requirements

# ---------------------------------------------------------------------------
# Model output types per stage


class ScriptOutput(BaseModel):
    episodes: list[Episode]


class StoryboardOutput(BaseModel):
    shots: list[Shot]
    timing: list[EpisodeTiming]
    style_brief: StyleBrief
    # Optional richer design text keyed by requirement_id; structure stays program-owned.
    design_overrides: list[AssetRequirement] = Field(default_factory=list)


class CreativeReviewOutput(BaseModel):
    issues: list[Issue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline state


@dataclass
class PipelineState:
    request: JobRequest
    package_id: str
    revision_id: str
    source_brief: SourceBrief | None = None
    story_bible: StoryBible | None = None
    episodes: list[Episode] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    timing: list[EpisodeTiming] = field(default_factory=list)
    asset_requirements: AssetRequirements | None = None
    qa_report: QAReport = field(default_factory=QAReport)
    model_versions: dict[str, str] = field(default_factory=dict)

    def draft_package(self) -> ProductionPackage:
        """Assemble whatever exists so far; missing parts become empty placeholders."""
        placeholder_style = StyleBrief(
            medium="", era_region="", color_and_light="", aspect_ratio=""
        )
        return ProductionPackage(
            package_id=self.package_id,
            revision_id=self.revision_id,
            source_brief=self.source_brief,  # type: ignore[arg-type]
            story_bible=self.story_bible,  # type: ignore[arg-type]
            episodes=self.episodes,
            shot_list=self.shots,
            timing=self.timing,
            asset_requirements=self.asset_requirements
            or AssetRequirements(style_brief=placeholder_style),
            experiment=Experiment(concept_id=self.package_id),
            provenance=Provenance(
                request=self.request,
                model_versions=self.model_versions,
                prompt_versions={"orchestrator": ORCHESTRATOR_PROMPT_VERSION},
            ),
            qa_report=self.qa_report,
        )

    def context(self) -> dict[str, Any]:
        """Upstream artifacts handed to the model as JSON."""
        ctx: dict[str, Any] = {"request": self.request.model_dump(mode="json")}
        if self.source_brief:
            ctx["source_brief"] = self.source_brief.model_dump(mode="json")
        if self.story_bible:
            ctx["story_bible"] = self.story_bible.model_dump(mode="json")
        if self.episodes:
            ctx["episodes"] = [e.model_dump(mode="json") for e in self.episodes]
        return ctx


# ---------------------------------------------------------------------------
# Steps


class Step:
    name: StageName
    output_model: type[BaseModel]
    directive: str

    def apply(self, state: PipelineState, output: Any) -> None:
        raise NotImplementedError

    def check(self, state: PipelineState) -> list[Issue]:
        raise NotImplementedError


class IngestStep(Step):
    name = "ingest"
    output_model = SourceBrief
    directive = (
        "Current step: understand the sources. Separate observed_facts, user_constraints and "
        "creative_assumptions. Record source_coverage for every source; mark unanalyzed "
        "ranges as unknown."
    )

    def apply(self, state: PipelineState, output: SourceBrief) -> None:
        state.source_brief = output

    def check(self, state: PipelineState) -> list[Issue]:
        brief = state.source_brief
        assert brief is not None
        covered = {c.source_index for c in brief.source_coverage}
        return [
            Issue(check="source_coverage", object_id=f"source[{i}]", evidence="no coverage record")
            for i in range(len(state.request.sources))
            if i not in covered
        ]


class OutlineStep(Step):
    name = "outline"
    output_model = StoryBible
    directive = (
        "Current step: design the story bible and a beat outline for all three episodes. "
        "Fix protagonist goal, opposing goal, obstacle, cost and causal progression first."
    )

    def apply(self, state: PipelineState, output: StoryBible) -> None:
        state.story_bible = output

    def check(self, state: PipelineState) -> list[Issue]:
        bible = state.story_bible
        assert bible is not None
        issues: list[Issue] = []
        n = state.request.options.episode_count
        if len(bible.beat_outlines) != n:
            issues.append(
                Issue(check="beat_outline", object_id="story_bible", evidence=f"need {n} outlines")
            )
        if state.request.options.story_scope == "series_opening" and not bible.future_direction:
            issues.append(
                Issue(check="future_direction", object_id="story_bible", evidence="missing")
            )
        return issues


class ScriptStep(Step):
    name = "script"
    output_model = ScriptOutput
    directive = (
        "Current step: write all three episodes. Every scene must change the situation; later "
        "episodes read the previous episode's end_state. Give every line a stable dialogue_id."
    )

    def apply(self, state: PipelineState, output: ScriptOutput) -> None:
        state.episodes = output.episodes

    def check(self, state: PipelineState) -> list[Issue]:
        pkg = state.draft_package()
        idx = PackageIndex.build(pkg)
        opts = state.request.options
        return (
            hc.check_unique_ids(pkg, idx, opts)
            + hc.check_episode_structure(pkg, idx, opts)
            + hc.check_references(pkg, idx, opts)
        )


class StoryboardStep(Step):
    name = "storyboard"
    output_model = StoryboardOutput
    directive = (
        "Current step: break scripts into a text shot list and episode-level dialogue "
        "schedule. Each episode timeline must total exactly the episode duration. Shots must "
        "not add plot. Mark needs_shot_reference only where a composition image is necessary."
    )

    def apply(self, state: PipelineState, output: StoryboardOutput) -> None:
        assert state.story_bible is not None
        state.shots = output.shots
        state.timing = output.timing
        state.asset_requirements = aggregate_asset_requirements(
            state.story_bible,
            state.shots,
            output.style_brief,
            {o.requirement_id: o for o in output.design_overrides},
        )
        valid = hc.compute_planned_timing_valid(state.draft_package(), state.request.options)
        for t in state.timing:
            t.planned_timing_valid = valid.get(t.episode_id, False)
            t.spoken_timing_verified = False  # no voice measurement in V1

    def check(self, state: PipelineState) -> list[Issue]:
        return hc.run_hard_checks(state.draft_package(), state.request.options)


class ReviewStep(Step):
    name = "review"
    output_model = CreativeReviewOutput
    directive = (
        "Current step: creative review. Report issues with object_id, evidence, severity and "
        "fix_scope. Do not describe scores as ad-performance predictions."
    )

    def apply(self, state: PipelineState, output: CreativeReviewOutput) -> None:
        # Hard checks already passed at the storyboard gate and review does not modify the
        # package, so they are not re-run here.
        state.qa_report = QAReport(hard_issues=[], creative_issues=output.issues)

    def check(self, state: PipelineState) -> list[Issue]:
        # Creative issues are advisory (spec section 8): reported to the user, never a gate.
        return []


STEPS: tuple[Step, ...] = (
    IngestStep(),
    OutlineStep(),
    ScriptStep(),
    StoryboardStep(),
    ReviewStep(),
)
