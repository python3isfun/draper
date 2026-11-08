"""Vendor-neutral model interface. Each pipeline step asks for one typed output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..models import Issue
from ..models.package import StageName

T = TypeVar("T", bound=BaseModel)


@dataclass
class GenerationRequest:
    stage: StageName
    instructions: str
    # Upstream artifacts, options and locks, already serialized to plain JSON-able data.
    context: dict[str, Any]
    # Issues from the previous attempt of this stage, for targeted repair.
    feedback: list[Issue] = field(default_factory=list)
    attempt: int = 0
    # Filled by the client: token counts summed over all HTTP calls for this request.
    usage: dict[str, int] = field(default_factory=dict)
    # Filled by the client: the model that actually served this request.
    model: str | None = None
    # Live trace hook (tracing.CallRecorder); None means nothing is recording.
    recorder: Any = None


class ModelClient(Protocol):
    model_id: str

    async def generate(self, request: GenerationRequest, output_model: type[T]) -> T:
        """Return an instance of output_model. Implementations should use structured output
        (JSON schema from output_model.model_json_schema()) and validate the response."""
        ...
