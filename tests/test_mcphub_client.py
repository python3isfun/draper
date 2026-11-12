"""MCPHub client: offline tests via httpx.MockTransport, plus an opt-in live test."""

import asyncio
import json
import os

import httpx
import pytest

from drama_agent.config import client_from_env, load_dotenv
from drama_agent.llm import FixtureModelClient, GenerationRequest
from drama_agent.llm.mcphub import (
    COMMENTARY_HINT,
    MCPHubClient,
    ModelClientError,
    ModelOutputInvalid,
    ModelRefusal,
)
from drama_agent.models import Issue, SourceBrief

from .conftest import run_pipeline

VALID_BRIEF = {
    "language": "en-US",
    "source_coverage": [{"source_index": 0}],
    "creative_brief": {
        "genre": "family",
        "audience_assumption": "adults",
        "core_conflict": "shop vs father",
        "emotional_experience": "tense",
    },
}


def _sse(events: list[dict]) -> httpx.Response:
    body = "".join(f"event: {e.get('type', 'chunk')}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _chunks(text: str, n: int = 7) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)] or [""]


def responses_sse(
    answer: str,
    *,
    commentary: str = "",
    reasoning: str = "",
    refusal: str = "",
    incomplete: str | None = None,
) -> httpx.Response:
    """A Responses API stream shaped like MCPHub's (see the live probe): a reasoning item,
    an optional commentary message, then the final_answer message."""
    ev: list[dict] = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "summary": []},
        }
    ]
    if reasoning:
        ev.append({"type": "response.reasoning_summary_text.delta", "delta": reasoning})
    idx = 1
    if commentary:
        ev.append(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "message", "phase": "commentary"},
            }
        )
        ev += [
            {"type": "response.output_text.delta", "output_index": 1, "delta": c}
            for c in _chunks(commentary)
        ]
        idx = 2
    ev.append(
        {
            "type": "response.output_item.added",
            "output_index": idx,
            "item": {"type": "message", "phase": "final_answer"},
        }
    )
    if refusal:
        ev.append({"type": "response.refusal.delta", "output_index": idx, "delta": refusal})
    ev += [
        {"type": "response.output_text.delta", "output_index": idx, "delta": c}
        for c in _chunks(answer)
    ]
    response = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 3},
        }
    }
    if incomplete:
        response["incomplete_details"] = {"reason": incomplete}
    ev.append(
        {
            "type": "response.incomplete" if incomplete else "response.completed",
            "response": response,
        }
    )
    return _sse(ev)


def chat_sse(answer: str, finish: str = "stop") -> httpx.Response:
    ev = [{"choices": [{"delta": {"role": "assistant", "content": ""}}]}]
    ev += [{"choices": [{"delta": {"content": c}}]} for c in _chunks(answer)]
    ev.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    ev.append(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        }
    )
    return _sse(ev)


class FakeRecorder:
    """Captures what the client reports to the live trace."""

    def __init__(self):
        self.calls: list[dict] = []
        self.notes: list[tuple[str, str]] = []

    @property
    def call(self):
        return None

    def start(self, **kw):
        self.calls.append({**kw, "output": "", "commentary": "", "reasoning": "", "end": None})

    def delta(self, channel, text):
        key = {"output": "output", "commentary": "commentary", "reasoning": "reasoning"}[channel]
        self.calls[-1][key] += text

    def finish(self, usage=None, error=None):
        self.calls[-1]["end"] = {"usage": usage, "error": error}

    def note(self, kind, message, **data):
        self.notes.append((kind, message))


def make_client(handler, api="responses") -> tuple[MCPHubClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req, len(seen))

    client = MCPHubClient(
        "https://hub.test/v1",
        "sk-test",
        "gpt-6.1-sol",
        api=api,
        backoff_s=0,
        transport=httpx.MockTransport(wrapped),
    )
    return client, seen


def request(**kw) -> GenerationRequest:
    return GenerationRequest(stage="ingest", instructions="SYS", context={"x": 1}, **kw)


def test_responses_stream_separates_commentary_from_answer():
    rec = FakeRecorder()
    client, seen = make_client(
        lambda r, n: responses_sse(
            json.dumps(VALID_BRIEF),
            commentary="First pin down the facts in the source, then build the conflict.",
            reasoning="summary text",
        )
    )
    req = request(feedback=[Issue(check="c", object_id="o", evidence="e")], recorder=rec)
    out = asyncio.run(client.generate(req, SourceBrief))

    assert isinstance(out, SourceBrief)  # commentary is not part of the parsed answer
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/responses"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert sent["model"] == "gpt-6.1-sol" and sent["stream"] is True
    assert sent["text"]["format"]["name"] == "SourceBrief"
    assert sent["reasoning"] == {"summary": "auto"}
    assert sent["instructions"].startswith("SYS") and COMMENTARY_HINT in sent["instructions"]
    assert "previous attempt" in sent["input"][0]["content"]  # feedback included
    assert req.usage == {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3}

    [call] = rec.calls
    assert call["api"] == "responses" and call["model"] == "gpt-6.1-sol" and call["turn"] == 0
    assert call["commentary"] == "First pin down the facts in the source, then build the conflict."
    assert call["reasoning"] == "summary text"
    assert json.loads(call["output"]) == VALID_BRIEF
    assert call["end"] == {"usage": req.usage, "error": None}


def test_invalid_json_gets_one_repair_turn():
    rec = FakeRecorder()

    def handler(r, n):
        body = {"language": "en-US"} if n == 1 else VALID_BRIEF
        return responses_sse(json.dumps(body))

    client, seen = make_client(handler)
    req = request(recorder=rec)
    out = asyncio.run(client.generate(req, SourceBrief))
    assert out.language == "en-US" and len(seen) == 2
    repair = json.loads(seen[1].content)["input"]
    assert repair[-2]["role"] == "assistant" and "failed schema validation" in repair[-1]["content"]
    assert req.usage["prompt_tokens"] == 20  # summed across both calls
    assert [c["turn"] for c in rec.calls] == [0, 1]
    assert rec.calls[0]["end"]["error"].startswith("schema validation failed")
    assert rec.calls[1]["end"]["error"] is None
    assert [k for k, _ in rec.notes] == ["schema_repair"]


def test_invalid_after_repair_raises():
    client, _ = make_client(lambda r, n: responses_sse("{}"))
    with pytest.raises(ModelOutputInvalid):
        asyncio.run(client.generate(request(), SourceBrief))


def test_retries_on_5xx_then_succeeds():
    rec = FakeRecorder()

    def handler(r, n):
        if n < 3:
            return httpx.Response(503, text="busy")
        return responses_sse(json.dumps(VALID_BRIEF))

    client, seen = make_client(handler)
    asyncio.run(client.generate(request(recorder=rec), SourceBrief))
    assert len(seen) == 3
    assert [k for k, _ in rec.notes] == ["model_retry", "model_retry"]
    # Each broken request is closed as failed; only the last one succeeded.
    assert [c["end"]["error"] is None for c in rec.calls] == [False, False, True]


def test_non_retryable_error_does_not_leak_key():
    rec = FakeRecorder()
    client, seen = make_client(lambda r, n: httpx.Response(401, text="bad key"))
    with pytest.raises(ModelClientError) as exc:
        asyncio.run(client.generate(request(recorder=rec), SourceBrief))
    assert len(seen) == 1 and "401" in str(exc.value) and "sk-test" not in str(exc.value)
    assert "401" in rec.calls[-1]["end"]["error"]


def test_refusal_truncation_and_failed_stream():
    client, _ = make_client(lambda r, n: responses_sse("", refusal="no"))
    with pytest.raises(ModelRefusal):
        asyncio.run(client.generate(request(), SourceBrief))
    client, _ = make_client(lambda r, n: responses_sse("{", incomplete="max_output_tokens"))
    with pytest.raises(ModelOutputInvalid, match="truncated"):
        asyncio.run(client.generate(request(), SourceBrief))
    client, _ = make_client(
        lambda r, n: _sse([{"type": "response.failed", "response": {"error": {"message": "x"}}}])
    )
    with pytest.raises(ModelClientError, match="response failed"):
        asyncio.run(client.generate(request(), SourceBrief))


def test_chat_api_streams_without_commentary_hint():
    rec = FakeRecorder()
    client, seen = make_client(lambda r, n: chat_sse(json.dumps(VALID_BRIEF)), api="chat")
    req = request(recorder=rec)
    asyncio.run(client.generate(req, SourceBrief))
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions" and sent["stream"] is True
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}
    assert req.usage == {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3}
    assert json.loads(rec.calls[0]["output"]) == VALID_BRIEF
    client, _ = make_client(lambda r, n: chat_sse("{", finish="length"), api="chat")
    with pytest.raises(ModelOutputInvalid, match="truncated"):
        asyncio.run(client.generate(request(), SourceBrief))


def test_client_selection_is_opt_in(monkeypatch):
    monkeypatch.delenv("DRAMA_MODEL_CLIENT", raising=False)
    monkeypatch.setenv("MCPHUB_KEY", "sk-test")
    assert isinstance(client_from_env(), FixtureModelClient)

    monkeypatch.setenv("DRAMA_MODEL_CLIENT", "mcphub")
    monkeypatch.delenv("MCPHUB_URL", raising=False)
    with pytest.raises(RuntimeError, match="MCPHUB_URL"):
        client_from_env()


def test_load_dotenv_strips_quotes_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A_URL=\"https://x/v1\"\nexport B_KEY='k'\n# c\nC_EXISTING=new\n")
    monkeypatch.delenv("A_URL", raising=False)
    monkeypatch.delenv("B_KEY", raising=False)
    monkeypatch.setenv("C_EXISTING", "old")
    load_dotenv(env)
    assert os.environ["A_URL"] == "https://x/v1"
    assert os.environ["B_KEY"] == "k"
    assert os.environ["C_EXISTING"] == "old"


def test_orchestrator_records_token_usage():
    class CountingFixture(FixtureModelClient):
        async def generate(self, request, output_model):
            request.usage["prompt_tokens"] = 7
            return await super().generate(request, output_model)

    job, repo = run_pipeline(CountingFixture())
    usage = repo.get_package(job.package_id).provenance.token_usage
    assert usage["script"] == {"prompt_tokens": 7}


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call MCPHub")
def test_live_ingest_step():
    load_dotenv()
    os.environ["DRAMA_MODEL_CLIENT"] = "mcphub"
    client = client_from_env()
    req = GenerationRequest(
        stage="ingest",
        instructions="Summarize the story seed into a source brief.",
        context={
            "request": {
                "sources": [{"type": "text", "text": "A young woman about to lose her bookstore"}]
            }
        },
    )
    out = asyncio.run(client.generate(req, SourceBrief))
    assert out.source_coverage and req.usage["completion_tokens"] > 0


# ---------------------------------------------------------------------------
# Per-step models


def test_stage_models_route_each_step_to_its_model():
    seen_models = []

    def handler(req, n):
        seen_models.append(json.loads(req.content)["model"])
        return responses_sse(json.dumps(VALID_BRIEF))

    transport = httpx.MockTransport(lambda r: handler(r, 0))
    client = MCPHubClient(
        "https://hub.test/v1",
        "sk-test",
        "gpt-6.1-sol",
        stage_models={"ingest": "gpt-6-luna"},
        transport=transport,
    )
    ingest = GenerationRequest(stage="ingest", instructions="", context={})
    review = GenerationRequest(stage="review", instructions="", context={})
    asyncio.run(client.generate(ingest, SourceBrief))
    asyncio.run(client.generate(review, SourceBrief))
    assert seen_models == ["gpt-6-luna", "gpt-6.1-sol"]
    assert (ingest.model, review.model) == ("gpt-6-luna", "gpt-6.1-sol")


def test_stage_models_from_env(monkeypatch):
    from drama_agent.config import stage_models_from_env

    for k in list(os.environ):
        if k.startswith("MCPHUB_MODEL_"):
            monkeypatch.delenv(k)
    assert stage_models_from_env() == {}

    monkeypatch.setenv("MCPHUB_MODEL_INGEST", "gpt-6-luna")
    monkeypatch.setenv("MCPHUB_MODEL_REVIEW", " gpt-6-luna ")
    monkeypatch.setenv("MCPHUB_MODEL_SCRIPT", "")  # empty = no override
    assert stage_models_from_env() == {"ingest": "gpt-6-luna", "review": "gpt-6-luna"}

    monkeypatch.setenv("MCPHUB_MODEL_SCRIPTS", "gpt-6-luna")  # typo
    with pytest.raises(RuntimeError, match="MCPHUB_MODEL_SCRIPTS"):
        stage_models_from_env()


def test_client_from_env_applies_stage_models(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MCPHUB_MODEL_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("DRAMA_MODEL_CLIENT", "mcphub")
    monkeypatch.setenv("MCPHUB_URL", "https://hub.test/v1")
    monkeypatch.setenv("MCPHUB_KEY", "sk-test")
    monkeypatch.setenv("MCPHUB_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("MCPHUB_MODEL_STORYBOARD", "gpt-6.1-terra")
    client = client_from_env()  # constructing makes no network call
    assert client.model_for("storyboard") == "gpt-6.1-terra"
    assert client.model_for("script") == "gpt-6.1-sol"


def test_provenance_records_the_model_each_step_used():
    class PerStage(FixtureModelClient):
        async def generate(self, request, output_model):
            out = await super().generate(request, output_model)
            request.model = "gpt-6-luna" if request.stage in ("ingest", "review") else "gpt-6.1-sol"
            return out

    job, repo = run_pipeline(PerStage())
    versions = repo.get_package(job.package_id).provenance.model_versions
    assert versions == {
        "ingest": "gpt-6-luna",
        "outline": "gpt-6.1-sol",
        "script": "gpt-6.1-sol",
        "cast": "gpt-6.1-sol",
        "sets": "gpt-6.1-sol",
        "props": "gpt-6.1-sol",
        "storyboard": "gpt-6.1-sol",
        "review": "gpt-6-luna",
    }


def test_meta_reports_stage_models():
    from fastapi.testclient import TestClient

    from drama_agent.api import create_app

    client = MCPHubClient(
        "https://hub.test/v1", "sk-test", "gpt-6.1-sol", stage_models={"review": "gpt-6-luna"}
    )
    meta = TestClient(create_app(client=client)).get("/v1/meta").json()
    assert meta == {
        "model_id": "gpt-6.1-sol",
        "client": "MCPHubClient",
        "stage_models": {"review": "gpt-6-luna"},
        "image_model": "fixture-image-v1",
        "image_client": "FixtureImageClient",
    }
