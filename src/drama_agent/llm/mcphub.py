"""Model client for MCPHub, an OpenAI-compatible chat completions gateway.

Structured output uses `response_format: json_schema` in non-strict mode: the Pydantic
models contain open maps (e.g. Character.relationships) that strict mode rejects. The
response is therefore validated locally, and validation errors are sent back to the
model for a bounded number of repair turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from .base import GenerationRequest, T

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ModelClientError(RuntimeError):
    """Base error for model calls. Messages never include the API key."""


class ModelRefusal(ModelClientError):
    pass


class ModelOutputInvalid(ModelClientError):
    pass


def build_messages(request: GenerationRequest) -> list[dict[str, str]]:
    parts = [
        "## Context (JSON)",
        json.dumps(request.context, ensure_ascii=False),
    ]
    if request.feedback:
        parts += [
            "## Issues from your previous attempt — fix only what they require",
            json.dumps([i.model_dump(mode="json") for i in request.feedback], ensure_ascii=False),
        ]
    parts.append("Respond with a single JSON object matching the response schema.")
    return [
        {"role": "system", "content": request.instructions},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


class MCPHubClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        stage_models: dict[str, str] | None = None,
        timeout_s: float = 600.0,
        max_http_retries: int = 3,
        max_repair_turns: int = 1,
        backoff_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MCPHub API key is empty")
        self.model_id = model  # default for any stage without an override
        self.stage_models = dict(stage_models or {})
        self.max_http_retries = max_http_retries
        self.max_repair_turns = max_repair_turns
        self.backoff_s = backoff_s
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def model_for(self, stage: str) -> str:
        return self.stage_models.get(stage, self.model_id)

    async def generate(self, request: GenerationRequest, output_model: type[T]) -> T:
        model = self.model_for(request.stage)
        request.model = model
        messages = build_messages(request)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": output_model.__name__,
                "strict": False,
                "schema": output_model.model_json_schema(),
            },
        }
        for turn in range(self.max_repair_turns + 1):
            data = await self._post(
                {"model": model, "messages": messages, "response_format": response_format}
            )
            self._record_usage(request, data)
            content = self._content(data)
            try:
                return output_model.model_validate_json(content)
            except ValidationError as exc:
                log.warning(
                    "stage %s: invalid %s (turn %d): %d errors",
                    request.stage,
                    output_model.__name__,
                    turn,
                    exc.error_count(),
                )
                if turn == self.max_repair_turns:
                    raise ModelOutputInvalid(
                        f"{output_model.__name__} failed validation after {turn + 1} "
                        f"attempts: {exc.errors(include_url=False)[:5]}"
                    ) from exc
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Your JSON failed schema validation. Return the full "
                        "corrected object. Errors:\n"
                        + json.dumps(exc.errors(include_url=False)[:20], default=str),
                    },
                ]
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_http_retries + 1):
            try:
                resp = await self._http.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                if attempt == self.max_http_retries:
                    raise ModelClientError(f"MCPHub transport error: {exc!r}") from exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRYABLE_STATUS or attempt == self.max_http_retries:
                    raise ModelClientError(f"MCPHub HTTP {resp.status_code}: {resp.text[:500]}")
            await asyncio.sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _content(data: dict[str, Any]) -> str:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise ModelRefusal(f"model refused: {message['refusal'][:300]}")
        if choice.get("finish_reason") == "length":
            raise ModelOutputInvalid("model output truncated (finish_reason=length)")
        content = message.get("content")
        if not content:
            raise ModelOutputInvalid("empty model response")
        return content

    @staticmethod
    def _record_usage(request: GenerationRequest, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens"):
            request.usage[key] = request.usage.get(key, 0) + int(usage.get(key) or 0)
