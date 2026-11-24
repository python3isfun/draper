"""The music clients: ACE-Step on MCPHub's gpu-music route, and the offline tone."""

import asyncio

import httpx
import pytest

from drama_agent.musicgen import (
    ACEStepClient,
    FixtureMusicClient,
    MusicGenError,
    MusicRequest,
    clamp_music_seconds,
    music_client_from_env,
)
from drama_agent.videogen import ffmpeg_available


def test_music_seconds_are_whole_and_within_the_route_limits():
    assert clamp_music_seconds(3) == 10
    assert clamp_music_seconds(45.2) == 46
    assert clamp_music_seconds(1000) == 240


def test_the_acestep_client_submits_a_job_polls_it_and_downloads_every_take():
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v3/gpu-music/acestep/jobs":
            return httpx.Response(201, json={"id": "m1", "status": "queued"})
        if request.url.path == "/api/v3/gpu-music/jobs/m1":
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json={"status": "queued", "last_message": "waiting"})
            return httpx.Response(200, json={
                "status": "completed",
                "audio_urls": ["https://files.test/a.wav", "https://files.test/b.wav"],
                "takes": [{"seed": 11, "url": "https://files.test/a.wav", "size_bytes": 3},
                          {"seed": 22, "url": "https://files.test/b.wav", "size_bytes": 3}],
            })  # fmt: skip
        if request.url.host == "files.test":
            return httpx.Response(200, content=b"WAV" + request.url.path.encode(),
                                  headers={"content-type": "audio/wav"})  # fmt: skip
        return httpx.Response(404)

    client = ACEStepClient("mcp_key", base_url="https://api.test", poll_s=0,
                           transport=httpx.MockTransport(handle))  # fmt: skip
    takes = asyncio.run(client.generate(MusicRequest(
        caption="guqin and strings", duration_s=60, bpm=72, key="D minor", seeds=[11, 22]
    )))  # fmt: skip
    assert [(t.seed, t.data, t.job_id) for t in takes] == [
        (11, b"WAV/a.wav", "m1"), (22, b"WAV/b.wav", "m1")]  # fmt: skip
    body = client.body(
        MusicRequest(caption="x", duration_s=60, bpm=72, key="D minor", seeds=[11, 22])
    )
    assert body == {"caption": "x", "duration_seconds": 60, "bpm": 72, "key": "D minor",
                    "seeds": [11, 22]}  # fmt: skip
    assert seen[0].headers["X-API-Key"] == "mcp_key" and "Authorization" not in seen[0].headers
    assert "bpm" not in client.body(MusicRequest(caption="x", duration_s=20))


def test_a_failed_or_lost_job_is_an_error():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "m2"})
        return httpx.Response(200, json={"status": "lost", "last_message": "gpu restarted"})

    client = ACEStepClient("k", base_url="https://api.test", poll_s=0,
                           transport=httpx.MockTransport(handle))  # fmt: skip
    with pytest.raises(MusicGenError, match="lost"):
        asyncio.run(client.generate(MusicRequest(caption="x", duration_s=20)))
    with pytest.raises(MusicGenError, match="caption"):
        asyncio.run(client.generate(MusicRequest(caption=" ", duration_s=20)))


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")
def test_the_fixture_makes_a_tone_per_seed():
    client = FixtureMusicClient()
    takes = asyncio.run(client.generate(MusicRequest(caption="x", duration_s=12, seeds=[1, 2])))
    assert [t.seed for t in takes] == [1, 2]
    assert all(t.data[:4] == b"RIFF" for t in takes)
    assert takes[0].data != takes[1].data
    assert client.calls[0].duration_s == 12


def test_the_client_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("DRAMA_MUSIC_CLIENT", raising=False)
    assert isinstance(music_client_from_env(), FixtureMusicClient)
    monkeypatch.setenv("DRAMA_MUSIC_CLIENT", "mcphub")
    monkeypatch.delenv("MCPHUB_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MCPHUB_KEY"):
        music_client_from_env()
    monkeypatch.setenv("MCPHUB_KEY", "k")
    assert isinstance(music_client_from_env(), ACEStepClient)
