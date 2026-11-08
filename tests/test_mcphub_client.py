"""MCPHub client: offline tests via httpx.MockTransport, plus an opt-in live test."""

import asyncio
import json
import os

import httpx
import pytest

from drama_agent.config import client_from_env, load_dotenv
from drama_agent.llm import FixtureModelClient, GenerationRequest
from drama_agent.llm.mcphub import MCPHubClient, ModelClientError, ModelOutputInvalid, ModelRefusal
from drama_agent.models import Issue, SourceBrief

from .conftest import run_pipeline

VALID_BRIEF = {
    "language": "zh-CN",
    "source_coverage": [{"source_index": 0}],
    "creative_brief": {
        "genre": "family",
        "audience_assumption": "adults",
        "core_conflict": "shop vs father",
        "emotional_experience": "tense",
    },
}


def completion(content: str, finish="stop", refusal=None) -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_client(handler) -> tuple[MCPHubClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req, len(seen))

    client = MCPHubClient(
        "https://hub.test/v1",
        "sk-test",
        "gpt-6.1-sol",
        backoff_s=0,
        transport=httpx.MockTransport(wrapped),
    )
    return client, seen


def request(**kw) -> GenerationRequest:
    return GenerationRequest(stage="ingest", instructions="SYS", context={"x": 1}, **kw)


def test_success_sends_schema_and_auth_and_records_usage():
    client, seen = make_client(
        lambda r, n: httpx.Response(200, json=completion(json.dumps(VALID_BRIEF)))
    )
    req = request(feedback=[Issue(check="c", object_id="o", evidence="e")])
    out = asyncio.run(client.generate(req, SourceBrief))

    assert isinstance(out, SourceBrief)
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert sent["model"] == "gpt-6.1-sol"
    assert sent["response_format"]["json_schema"]["name"] == "SourceBrief"
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}
    assert "previous attempt" in sent["messages"][1]["content"]  # feedback included
    assert req.usage == {"prompt_tokens": 10, "completion_tokens": 5}


def test_invalid_json_gets_one_repair_turn():
    def handler(r, n):
        body = {"language": "zh-CN"} if n == 1 else VALID_BRIEF
        return httpx.Response(200, json=completion(json.dumps(body)))

    client, seen = make_client(handler)
    req = request()
    out = asyncio.run(client.generate(req, SourceBrief))
    assert out.language == "zh-CN" and len(seen) == 2
    repair = json.loads(seen[1].content)["messages"]
    assert repair[-2]["role"] == "assistant" and "failed schema validation" in repair[-1]["content"]
    assert req.usage["prompt_tokens"] == 20  # summed across both calls


def test_invalid_after_repair_raises():
    client, _ = make_client(lambda r, n: httpx.Response(200, json=completion("{}")))
    with pytest.raises(ModelOutputInvalid):
        asyncio.run(client.generate(request(), SourceBrief))


def test_retries_on_5xx_then_succeeds():
    def handler(r, n):
        if n < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=completion(json.dumps(VALID_BRIEF)))

    client, seen = make_client(handler)
    asyncio.run(client.generate(request(), SourceBrief))
    assert len(seen) == 3


def test_non_retryable_error_does_not_leak_key():
    client, seen = make_client(lambda r, n: httpx.Response(401, text="bad key"))
    with pytest.raises(ModelClientError) as exc:
        asyncio.run(client.generate(request(), SourceBrief))
    assert len(seen) == 1 and "401" in str(exc.value) and "sk-test" not in str(exc.value)


def test_refusal_and_truncation():
    client, _ = make_client(lambda r, n: httpx.Response(200, json=completion("", refusal="no")))
    with pytest.raises(ModelRefusal):
        asyncio.run(client.generate(request(), SourceBrief))
    client, _ = make_client(lambda r, n: httpx.Response(200, json=completion("{", finish="length")))
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
        context={"request": {"sources": [{"type": "text", "text": "一个即将失去店铺的年轻人"}]}},
    )
    out = asyncio.run(client.generate(req, SourceBrief))
    assert out.source_coverage and req.usage["completion_tokens"] > 0


# ---------------------------------------------------------------------------
# Per-step models


def test_stage_models_route_each_step_to_its_model():
    seen_models = []

    def handler(req, n):
        seen_models.append(json.loads(req.content)["model"])
        return httpx.Response(200, json=completion(json.dumps(VALID_BRIEF)))

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
    }
