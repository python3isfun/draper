"""Production package, QA report, jobs and revisions (spec sections 6E, 8, 9)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .assets import AssetRequirements
from .request import JobRequest
from .screenplay import Episode
from .source_brief import SourceBrief
from .story import StoryBible
from .storyboard import EpisodeTiming, Shot


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# QA (spec section 8)

Severity = Literal["blocker", "major", "minor"]


class Issue(BaseModel):
    check: str
    object_id: str
    evidence: str
    severity: Severity = "blocker"
    fix_scope: str = ""


class QAReport(BaseModel):
    hard_issues: list[Issue] = Field(default_factory=list)
    creative_issues: list[Issue] = Field(default_factory=list)

    @property
    def hard_checks_passed(self) -> bool:
        return not self.hard_issues


# ---------------------------------------------------------------------------
# Package (spec section 6, 9)


class Experiment(BaseModel):
    concept_id: str
    hypothesis: str = ""
    opening_strategy: str = ""
    cliffhanger_strategy: str = ""
    primary_variable: str = ""
    variant_label: str | None = None
    # Links to downstream render / ad systems, filled later.
    render_ids: list[str] = Field(default_factory=list)
    creative_ids: list[str] = Field(default_factory=list)
    performance_data_ref: str | None = None


class Provenance(BaseModel):
    request: JobRequest
    model_versions: dict[str, str] = Field(default_factory=dict)  # stage -> model id
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    cost_usd: float = 0.0
    # stage -> {"prompt_tokens": n, "completion_tokens": n}, summed over retries.
    token_usage: dict[str, dict[str, int]] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ChangeLogEntry(BaseModel):
    revision_id: str
    target_ids: list[str]
    changed_ids: list[str]
    note: str


class ProductionPackage(BaseModel):
    package_id: str
    revision_id: str
    source_brief: SourceBrief
    story_bible: StoryBible
    episodes: list[Episode]
    shot_list: list[Shot]
    timing: list[EpisodeTiming]
    asset_requirements: AssetRequirements
    experiment: Experiment
    provenance: Provenance
    qa_report: QAReport = Field(default_factory=QAReport)
    locked_ids: list[str] = Field(default_factory=list)
    stale_ids: list[str] = Field(default_factory=list)
    change_log: list[ChangeLogEntry] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Jobs (spec section 9)

# "paused" extends the spec's statuses: the user paused the job; it resumes on request.
JobStatus = Literal[
    "queued", "running", "paused", "completed", "needs_revision", "failed", "cancelled"
]
StageName = Literal["ingest", "outline", "script", "storyboard", "review"]
StageStatus = Literal["pending", "running", "passed", "failed"]
STAGES: tuple[StageName, ...] = ("ingest", "outline", "script", "storyboard", "review")


class StageState(BaseModel):
    status: StageStatus = "pending"
    attempts: int = 0
    issues: list[Issue] = Field(default_factory=list)
    error: str | None = None


class Job(BaseModel):
    job_id: str
    request: JobRequest
    status: JobStatus = "queued"
    stages: dict[StageName, StageState] = Field(
        default_factory=lambda: {s: StageState() for s in STAGES}
    )
    package_id: str | None = None
    revision_id: str | None = None
    failure_reason: str | None = None
    # Execution attempts by the job queue (each claim by a worker is one attempt).
    attempts: int = 0
    # Set on API responses from the queue's pause flag; not authoritative when stored
    # (the flag lives in the queue so a running worker can't overwrite it).
    pause_requested: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Revisions (spec section 9)


class RevisionRequest(BaseModel):
    base_revision_id: str
    target_ids: list[str] = Field(min_length=1)
    instruction: str
    locked_ids: list[str] = Field(default_factory=list)
    variant_label: str | None = None
    # Extension to the spec draft: whether a character/location/prop edit changes its
    # appearance (invalidates art requirements) or only story facts.
    visual_change: bool = False


RevisionStatus = Literal["queued", "running", "published", "needs_revision", "failed"]


class Revision(BaseModel):
    revision_id: str
    package_id: str
    base_revision_id: str | None
    request: RevisionRequest | None = None
    status: RevisionStatus = "queued"
    impacted_ids: list[str] = Field(default_factory=list)
    stale_requirement_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    # Locked objects inside the scope; regeneration must leave them unchanged.
    locked_in_scope: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Trace: what the agent did, for live progress and later debugging


class TraceEvent(BaseModel):
    seq: int = 0  # assigned by the store; strictly increasing per store
    job_id: str
    ts: datetime = Field(default_factory=utcnow)
    kind: str  # e.g. stage_started, gate_issues, stage_passed, paused
    stage: str | None = None
    attempt: int | None = None
    message: str
    data: dict = Field(default_factory=dict)


LLMCallStatus = Literal["running", "succeeded", "failed"]


class LLMCall(BaseModel):
    """One request to the model (a schema-repair turn is its own call)."""

    call_id: str
    job_id: str
    stage: str
    attempt: int  # step attempt (0-based) within the job run
    turn: int = 0  # schema-repair turn within the attempt
    model: str
    api: str = ""
    status: LLMCallStatus = "running"
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    request: dict = Field(default_factory=dict)  # instructions + input as sent
    commentary: str = ""  # the model's progress notes (Responses API "commentary")
    reasoning_summary: str = ""  # only if the gateway returns summaries
    output: str = ""  # the answer text (JSON), streamed in
    output_chars: int = 0  # filled in list views, where `output` itself is omitted
    usage: dict = Field(default_factory=dict)
    error: str | None = None
