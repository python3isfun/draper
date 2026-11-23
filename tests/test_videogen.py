"""The MiniMax video client: a task is created, waited for and downloaded."""

import asyncio
import base64
import json

import httpx
import pytest

from drama_agent.videogen import (
    MCPHubVideoClient,
    MiniMaxVideoClient,
    VideoGenError,
    VideoReference,
    clamp_seconds,
)


def test_clip_seconds_are_whole_and_within_h3_limits():
    assert clamp_seconds(2500) == 4
    assert clamp_seconds(6001) == 7
    assert clamp_seconds(9000) == 9
    assert clamp_seconds(40000) == 15


def test_the_client_creates_a_task_polls_it_and_downloads_the_clip():
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v2/video_generation":
            return httpx.Response(200, json={"task_id": "t1"})
        if request.url.path == "/v2/query/video_generation/t1":
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json={"task": {"status": "running"}})
            return httpx.Response(
                200,
                json={
                    "task": {"status": "succeeded", "content": {"url": "https://files.test/c.mp4"}}
                },
            )
        if request.url.host == "files.test":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404)

    client = MiniMaxVideoClient("key", base_url="https://api.test", poll_s=0,
                                transport=httpx.MockTransport(handle))  # fmt: skip
    # The download goes through its own client; patch httpx for it.
    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc]
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handle)
            super().__init__(**kw)

    httpx.AsyncClient = Patched  # type: ignore[misc]
    try:
        out = asyncio.run(client.generate(
            "[Shot 1] The woman from <Picture 1> waits.", 7, "9:16",
            [VideoReference("kf.jpg", b"\xff\xd8jpeg", "image/jpeg")]))  # fmt: skip
    finally:
        httpx.AsyncClient = real  # type: ignore[misc]
    assert out.data == b"MP4DATA" and out.task_id == "t1" and out.model == "MiniMax-H3"
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer key"
    assert body["model"] == "MiniMax-H3" and body["duration"] == 7 and body["ratio"] == "9:16"
    assert body["resolution"] == "768P" and body["generate_audio"] is True
    text, pic = body["content"]
    assert text == {"type": "text", "text": "[Shot 1] The woman from <Picture 1> waits."}
    assert pic["type"] == "image_url" and pic["role"] == "reference_image"
    assert (
        pic["image_url"]["url"]
        == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode()
    )


def test_a_failed_task_and_a_refusal_are_errors_without_the_key():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/video_generation":
            if b"refuse" in request.content:
                return httpx.Response(400, json={"error": {"message": "bad prompt"}})
            return httpx.Response(200, json={"task_id": "t2"})
        return httpx.Response(200, json={"task": {"status": "failed", "error": "content policy"}})

    client = MiniMaxVideoClient("secret-key", base_url="https://api.test", poll_s=0,
                                transport=httpx.MockTransport(handle))  # fmt: skip
    refs = [VideoReference("a.jpg", b"x")]
    with pytest.raises(VideoGenError, match="content policy") as exc:
        asyncio.run(client.generate("ok", 5, "9:16", refs))
    assert "secret-key" not in str(exc.value)
    with pytest.raises(VideoGenError, match="refused"):
        asyncio.run(client.generate("refuse", 5, "9:16", refs))
    with pytest.raises(VideoGenError, match="reference picture"):
        asyncio.run(client.generate("ok", 5, "9:16", []))


def test_mcphub_client_submits_a_job_and_polls_it():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/gpu-videos/minimax-h3-ref2va/jobs":
            seen.append(request)
            return httpx.Response(202, json={"id": "job1", "status": "queued"})
        if request.url.path == "/api/v3/gpu-videos/jobs/job1":
            seen.append(request)
            if len(seen) < 3:
                return httpx.Response(200, json={"id": "job1", "status": "running"})
            return httpx.Response(200, json={"id": "job1", "status": "completed",
                                             "video_url": "https://files.test/c.mp4"})  # fmt: skip
        if request.url.host == "files.test":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404)

    client = MCPHubVideoClient("mcp_key", base_url="https://api.test", seed=7, poll_s=0,
                               transport=httpx.MockTransport(handle))  # fmt: skip
    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc]
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handle)
            super().__init__(**kw)

    httpx.AsyncClient = Patched  # type: ignore[misc]
    try:
        out = asyncio.run(client.generate(
            "[Shot 1] She waits.", 8, "9:16", [VideoReference("kf.png", b"\x89PNG", "image/png")]))  # fmt: skip
    finally:
        httpx.AsyncClient = real  # type: ignore[misc]
    assert out.data == b"MP4DATA" and out.task_id == "job1" and out.model == "minimax-h3-ref2va"
    assert seen[0].headers["x-api-key"] == "mcp_key" and "authorization" not in seen[0].headers
    body = json.loads(seen[0].content)
    assert (
        body["task"] == "ref2va" and body["seed"] == 7 and body["prompt"] == "[Shot 1] She waits."
    )
    assert body["target"] == {"short_edge": 768, "aspect_ratio": "9:16", "duration_seconds": 8}
    assert body["conditions"] == [{"type": "image", "role": "reference",
                                   "uri": "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()}]  # fmt: skip
    assert len(seen) == 3
