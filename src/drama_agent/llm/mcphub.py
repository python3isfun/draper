"""Model client for MCPHub, an OpenAI-compatible gateway, with streaming.

Two wire APIs (MCPHUB_API):

- `responses` (default): the OpenAI Responses API. Besides the JSON answer it streams the
  model's *commentary*: short plain-language progress notes, kept separate from the answer.
  Raw reasoning is encrypted by the provider and not available; reasoning summaries are
  requested and recorded when the gateway returns them.
- `chat`: Chat Completions with streaming. Live output, but no commentary.

Structured output uses a JSON schema in non-strict mode: the Pydantic models contain open
maps (e.g. Character.relationships) that strict mode rejects. The answer is validated
locally and validation errors get a bounded number of repair turns. Every request is
reported to the GenerationRequest's recorder for the live trace.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from ..tracing import NULL_RECORDER
from .base import GenerationRequest, T

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
WireAPI = Literal["responses", "chat"]

# Responses API only: asks for the commentary notes the UI shows while a step runs.
COMMENTARY_HINT = (
    "While you work, share brief progress notes (one or two plain sentences each, in the "
    "story's output language) about what you are deciding and why; they are shown to the "
    "user live. Your final answer must be only the JSON object."
)


class ModelClientError(RuntimeError):
    """Base error for model calls. Messages never include the API key."""


class ModelRefusal(ModelClientError):
    pass


class ModelOutputInvalid(ModelClientError):
    pass


class _Retryable(Exception):
    """A failure worth retrying the HTTP request for (network, 5xx, 429)."""


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


def _data_url(part: Any) -> str:
    data = base64.b64encode(Path(part.path).read_bytes()).decode()
    return f"data:{part.mime};base64,{data}"


def with_media(
    messages: list[dict[str, Any]],
    images: list[tuple[Any, str]],
    api: WireAPI,
    *,
    inline: bool = True,
) -> list[dict[str, Any]]:
    """Attach images to the first user message, each preceded by its label.

    inline=False gives the version kept in the trace: labels and file names instead of
    base64 data, so a recorded call stays small and readable.
    """
    if not images:
        return messages
    text_type, image_type = (
        ("input_text", "input_image") if api == "responses" else ("text", "image_url")
    )
    content: list[dict[str, Any]] = [{"type": text_type, "text": messages[1]["content"]}]
    for part, url in images:
        content.append({"type": text_type, "text": part.label})
        if not inline:
            content.append({"type": "image", "file": Path(part.path).name})
        elif api == "responses":
            content.append({"type": image_type, "image_url": url})
        else:
            content.append({"type": image_type, "image_url": {"url": url}})
    return [messages[0], {**messages[1], "content": content}, *messages[2:]]


@dataclass
class _Streamed:
    text: str = ""  # the answer (JSON)
    usage: dict[str, int] = field(default_factory=dict)
    refusal: str = ""
    truncated: str | None = None  # reason, if the output was cut off


def _usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize Responses (input/output_tokens) and Chat (prompt/completion_tokens)."""
    raw = raw or {}
    details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("output_tokens") or raw.get("completion_tokens") or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
    }


class MCPHubClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        stage_models: dict[str, str] | None = None,
        api: WireAPI = "responses",
        transcribe_model: str = "whisper-1",
        timeout_s: float = 600.0,
        call_timeout_s: float = 1200.0,
        max_http_retries: int = 3,
        max_repair_turns: int = 1,
        backoff_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MCPHub API key is empty")
        if api not in ("responses", "chat"):
            raise ValueError(f"unknown MCPHub api {api!r}")
        self.model_id = model  # default for any stage without an override
        self.stage_models = dict(stage_models or {})
        self.api: WireAPI = api
        self.transcribe_model = transcribe_model
        self.max_http_retries = max_http_retries
        # Whole-call deadline. httpx's timeout counts the gap between bytes, and MCPHub keeps
        # an idle stream open with keep-alive lines, so without this a stalled call hangs
        # forever (seen live: a storyboard call silent for 48 min). Storyboard calls, the
        # longest, take 5-8 min.
        self.call_timeout_s = call_timeout_s
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
        rec = request.recorder or NULL_RECORDER
        messages = build_messages(request)
        if self.api == "responses":
            messages[0] = {
                **messages[0],
                "content": f"{messages[0]['content']}\n\n{COMMENTARY_HINT}",
            }
        schema = output_model.model_json_schema()
        images = await asyncio.to_thread(  # read and encoded once per request
            lambda: [(part, _data_url(part)) for part in request.media]
        )

        for turn in range(self.max_repair_turns + 1):
            recorded = {
                "messages": with_media(messages, images, self.api, inline=False),
                "schema": output_model.__name__,
            }
            rec.start(model=model, api=self.api, request=recorded, turn=turn)
            wire = with_media(messages, images, self.api)
            try:
                result = await self._stream_with_retries(
                    model, wire, recorded, output_model, schema, rec
                )
            except asyncio.CancelledError:
                rec.finish(error="cancelled before the response finished")
                raise
            except Exception as exc:
                rec.finish(error=str(exc) or type(exc).__name__)
                raise
            for k, v in result.usage.items():
                request.usage[k] = request.usage.get(k, 0) + v

            error: ModelClientError | None = None
            if result.refusal:
                error = ModelRefusal(f"model refused: {result.refusal[:300]}")
            elif result.truncated:
                error = ModelOutputInvalid(f"model output truncated ({result.truncated})")
            elif not result.text.strip():
                error = ModelOutputInvalid("empty model response")
            if error:
                rec.finish(usage=result.usage, error=str(error))
                raise error

            try:
                parsed = output_model.model_validate_json(result.text)
            except ValidationError as exc:
                summary = f"{exc.error_count()} schema errors"
                log.warning(
                    "stage %s: invalid %s (turn %d): %s",
                    request.stage,
                    output_model.__name__,
                    turn,
                    summary,
                )
                rec.finish(usage=result.usage, error=f"schema validation failed: {summary}")
                if turn == self.max_repair_turns:
                    raise ModelOutputInvalid(
                        f"{output_model.__name__} failed validation after {turn + 1} "
                        f"attempts: {exc.errors(include_url=False)[:5]}"
                    ) from exc
                rec.note(
                    "schema_repair",
                    f"Answer didn't match the schema ({summary}); asking the model to fix it.",
                    errors=[str(e.get("loc")) for e in exc.errors(include_url=False)[:10]],
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": result.text},
                    {
                        "role": "user",
                        "content": "Your JSON failed schema validation. Return the full "
                        "corrected object. Errors:\n"
                        + json.dumps(exc.errors(include_url=False)[:20], default=str),
                    },
                ]
                continue
            rec.finish(usage=result.usage)
            return parsed
        raise AssertionError("unreachable")

    async def transcribe(self, audio_path: Path) -> list[dict[str, Any]]:
        """Speech to text with segment timestamps (whisper verbose_json)."""
        audio = await asyncio.to_thread(Path(audio_path).read_bytes)
        last: Exception | None = None
        for attempt in range(self.max_http_retries + 1):
            try:
                resp = await self._http.post(
                    "/audio/transcriptions",
                    data={"model": self.transcribe_model, "response_format": "verbose_json"},
                    files={"file": (Path(audio_path).name, audio, "audio/mpeg")},
                )
            except httpx.TransportError as exc:
                last = exc
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    segments = body.get("segments") or []
                    if not segments and body.get("text"):  # model without timestamps
                        dur = float(body.get("duration") or 0)
                        segments = [{"start": 0, "end": dur, "text": body["text"]}]
                    return [
                        {
                            "start_ms": round(float(seg.get("start") or 0) * 1000),
                            "end_ms": round(float(seg.get("end") or 0) * 1000),
                            "text": str(seg.get("text") or "").strip(),
                        }
                        for seg in segments
                        if str(seg.get("text") or "").strip()
                    ]
                msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ModelClientError(f"MCPHub transcription {msg}")
                last = RuntimeError(msg)
            if attempt < self.max_http_retries:
                await asyncio.sleep(self.backoff_s * 2**attempt)
        raise ModelClientError(f"MCPHub transcription failed: {last}")

    # ------------------------------------------------------------------
    # Transport

    async def _stream_with_retries(
        self, model, messages, recorded, output_model, schema, rec
    ) -> _Streamed:
        for attempt in range(self.max_http_retries + 1):
            try:
                async with asyncio.timeout(self.call_timeout_s):
                    if self.api == "responses":
                        return await self._stream_responses(
                            model, messages, output_model, schema, rec
                        )
                    return await self._stream_chat(model, messages, output_model, schema, rec)
            except TimeoutError as exc:
                if attempt == self.max_http_retries:
                    raise ModelClientError(
                        f"MCPHub call gave no complete response within {self.call_timeout_s:.0f}s"
                    ) from exc
                delay = self.backoff_s * 2**attempt
                rec.finish(
                    error=f"no complete response within {self.call_timeout_s:.0f}s; retrying"
                )
                rec.note(
                    "model_retry",
                    f"The model call stalled (no complete response in "
                    f"{self.call_timeout_s / 60:.0f} min); retrying in {delay:.0f}s.",
                )
                rec.start(
                    model=model,
                    api=self.api,
                    request=recorded,
                    turn=getattr(getattr(rec, "call", None), "turn", 0),
                )
                await asyncio.sleep(delay)
            except _Retryable as exc:
                if attempt == self.max_http_retries:
                    raise ModelClientError(f"MCPHub request failed: {exc}") from exc
                delay = self.backoff_s * 2**attempt
                rec.finish(error=f"{exc}; retrying")  # close the broken call's record
                rec.note("model_retry", f"Model request failed ({exc}); retrying in {delay:.0f}s.")
                rec.start(  # a fresh call record; the broken stream's partial text is dropped
                    model=model,
                    api=self.api,
                    request=recorded,
                    turn=getattr(getattr(rec, "call", None), "turn", 0),
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _sse(self, path: str, payload: dict[str, Any]):
        """Yield parsed JSON objects from a server-sent-events response."""
        try:
            async with self._http.stream("POST", path, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:500]
                    msg = f"HTTP {resp.status_code}: {body}"
                    if resp.status_code in RETRYABLE_STATUS:
                        raise _Retryable(msg)
                    raise ModelClientError(f"MCPHub {msg}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("unparseable stream line: %s", data[:200])
        except httpx.TransportError as exc:
            raise _Retryable(f"transport error: {exc!r}") from exc

    async def _stream_responses(self, model, messages, output_model, schema, rec) -> _Streamed:
        payload = {
            "model": model,
            "instructions": messages[0]["content"],
            "input": messages[1:],
            "stream": True,
            "reasoning": {"summary": "auto"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": output_model.__name__,
                    "strict": False,
                    "schema": schema,
                }
            },
        }
        out = _Streamed()
        phases: dict[int, str | None] = {}  # output_index -> message phase
        answers: dict[int, str] = {}  # output_index -> text of non-commentary messages
        async for ev in self._sse("/responses", payload):
            kind = ev.get("type", "")
            if kind == "response.output_item.added":
                item = ev.get("item") or {}
                if item.get("type") == "message":
                    phases[ev.get("output_index", 0)] = item.get("phase")
                    if (
                        item.get("phase") == "commentary"
                        and getattr(rec, "call", None)
                        and rec.call.commentary
                    ):
                        rec.delta("commentary", "\n\n")  # separate successive notes
            elif kind == "response.output_text.delta":
                idx = ev.get("output_index", 0)
                text = ev.get("delta", "")
                if phases.get(idx) == "commentary":
                    rec.delta("commentary", text)
                else:
                    answers[idx] = answers.get(idx, "") + text
                    rec.delta("output", text)
            elif kind == "response.reasoning_summary_text.delta":
                rec.delta("reasoning", ev.get("delta", ""))
            elif kind == "response.refusal.delta":
                out.refusal += ev.get("delta", "")
            elif kind in ("response.completed", "response.incomplete"):
                response = ev.get("response") or {}
                out.usage = _usage(response.get("usage"))
                if kind == "response.incomplete":
                    reason = (response.get("incomplete_details") or {}).get("reason")
                    out.truncated = reason or "incomplete"
            elif kind in ("response.failed", "error"):
                err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                raise ModelClientError(f"MCPHub response failed: {str(err)[:300]}")
        finals = [i for i, p in phases.items() if p == "final_answer"]
        keys = finals or sorted(answers)[-1:]
        out.text = "".join(answers.get(i, "") for i in sorted(keys))
        return out

    async def _stream_chat(self, model, messages, output_model, schema, rec) -> _Streamed:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": output_model.__name__, "strict": False, "schema": schema},
            },
        }
        out = _Streamed()
        async for chunk in self._sse("/chat/completions", payload):
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    out.text += delta["content"]
                    rec.delta("output", delta["content"])
                if delta.get("refusal"):
                    out.refusal += delta["refusal"]
                if choice.get("finish_reason") == "length":
                    out.truncated = "max tokens"
            if chunk.get("usage"):
                out.usage = _usage(chunk["usage"])
        return out
