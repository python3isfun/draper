"""Live trace of a job: agent steps (events) and every model call, as they happen.

Used by the UI to show progress while a job runs, and kept for debugging a story later.
Tracing is best-effort: a failed trace write is logged and never fails the job.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Literal

from .models import LLMCall, TraceEvent
from .models.package import utcnow

log = logging.getLogger(__name__)

Channel = Literal["output", "commentary", "reasoning"]


class Tracer:
    def __init__(self, repo: Any, job_id: str) -> None:
        self.repo = repo
        self.job_id = job_id

    def event(
        self,
        kind: str,
        message: str,
        *,
        stage: str | None = None,
        attempt: int | None = None,
        **data: Any,
    ) -> None:
        try:
            self.repo.add_event(
                TraceEvent(
                    job_id=self.job_id,
                    kind=kind,
                    stage=stage,
                    attempt=attempt,
                    message=message,
                    data=data,
                )
            )
        except Exception:  # tracing must never break a job
            log.exception("trace event %s for job %s not saved", kind, self.job_id)

    def recorder(self, stage: str, attempt: int) -> CallRecorder:
        return CallRecorder(self, stage, attempt)


class CallRecorder:
    """Handed to the model client via GenerationRequest.recorder. One recorder per step
    attempt; each HTTP request (including schema-repair turns) becomes one LLMCall."""

    def __init__(self, tracer: Tracer, stage: str, attempt: int, flush_s: float = 0.75) -> None:
        self.tracer = tracer
        self.stage = stage
        self.attempt = attempt
        self.flush_s = flush_s
        self.call: LLMCall | None = None
        self._last_flush = 0.0

    def start(self, *, model: str, api: str, request: dict, turn: int = 0) -> None:
        self.call = LLMCall(
            call_id=f"call_{uuid.uuid4().hex[:12]}",
            job_id=self.tracer.job_id,
            stage=self.stage,
            attempt=self.attempt,
            turn=turn,
            model=model,
            api=api,
            request=request,
        )
        self._save()

    def delta(self, channel: Channel, text: str) -> None:
        if self.call is None or not text:
            return
        if channel == "output":
            self.call.output += text
        elif channel == "commentary":
            self.call.commentary += text
        else:
            self.call.reasoning_summary += text
        # Throttled: streaming can produce hundreds of deltas per second.
        if time.monotonic() - self._last_flush >= self.flush_s:
            self._save()

    def finish(self, *, usage: dict | None = None, error: str | None = None) -> None:
        if self.call is None:
            return
        self.call.status = "failed" if error else "succeeded"
        self.call.error = error
        self.call.usage = usage or {}
        self.call.finished_at = utcnow()
        self._save()

    def note(self, kind: str, message: str, **data: Any) -> None:
        """An event tied to this step attempt (e.g. a schema-repair turn)."""
        self.tracer.event(kind, message, stage=self.stage, attempt=self.attempt, **data)

    def _save(self) -> None:
        if self.call is None:
            return
        self.call.updated_at = utcnow()
        self._last_flush = time.monotonic()
        try:
            self.tracer.repo.save_llm_call(self.call)
        except Exception:
            log.exception("llm call %s not saved", self.call.call_id)


class NullRecorder:
    """Used when nothing is tracing (unit tests, ad-hoc scripts)."""

    def start(self, **_: Any) -> None: ...
    def delta(self, channel: Channel, text: str) -> None: ...
    def finish(self, **_: Any) -> None: ...
    def note(self, kind: str, message: str, **data: Any) -> None: ...


NULL_RECORDER = NullRecorder()
