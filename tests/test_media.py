"""Image/video upload, keyframes, transcription and what the model is shown."""

import asyncio
import json
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.llm import FixtureModelClient
from drama_agent.llm.mcphub import MCPHubClient
from drama_agent.media import MediaLimits, MediaPart, MediaStore, ffmpeg_available, sniff
from drama_agent.models import Job, JobRequest, SourceAsset, SourceBrief, SourceCoverage
from drama_agent.models.source_brief import TimeRange
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.pipeline.steps import STEPS, PipelineState
from drama_agent.store import InMemoryRepository

from .conftest import wait_job
from .test_mcphub_client import VALID_BRIEF, FakeRecorder, make_client, request, responses_sse

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    if not ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=2000x1000", "-frames:v", "1", str(d / "big.png"))
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-shortest", "-pix_fmt", "yuv420p", str(d / "clip.mp4"),
    )  # fmt: skip
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(d / "silent.mp4"),
    )  # fmt: skip
    return {
        "image": (d / "big.png").read_bytes(),
        "video": (d / "clip.mp4").read_bytes(),
        "silent": (d / "silent.mp4").read_bytes(),
    }


def upload(http, data: bytes, filename: str):
    return http.post("/v1/source-assets", params={"filename": filename}, content=data)


def test_sniff_recognizes_supported_types_only():
    assert sniff(b"\xff\xd8\xff\xe0" + b"\0" * 8) == "image/jpeg"
    assert sniff(b"\x89PNG\r\n\x1a\n\0\0\0\0") == "image/png"
    assert sniff(b"RIFF\0\0\0\0WEBP") == "image/webp"
    assert sniff(b"\0\0\0\x18ftypisom") == "video/mp4"
    assert sniff(b"\0\0\0\x14ftypqt  ") == "video/quicktime"
    assert sniff(b"\x1a\x45\xdf\xa3\0\0\0\0\0\0\0\0") == "video/webm"
    assert sniff(b"<html><script>") is None
    assert sniff(b"%PDF-1.7 hello") is None


# ---------------------------------------------------------------------------
# Upload API


@needs_ffmpeg
def test_image_upload_is_stored_and_downscaled_for_the_model(tmp_path, media_files):
    store = MediaStore(tmp_path)
    with TestClient(create_app(workers=0, media=store)) as http:
        r = upload(http, media_files["image"], "../../etc/passwd.png")
        assert r.status_code == 201, r.text
        asset = r.json()
        assert asset["kind"] == "image" and asset["content_type"] == "image/png"
        assert asset["filename"] == "passwd.png"  # display name only, never a path
        assert (asset["width"], asset["height"]) == (2000, 1000)
        assert asset["model_image"].endswith("model.jpg")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0",
             str(store.path(asset["model_image"]))],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        assert int(probe.stdout) == 1536

        content = http.get(f"/v1/source-assets/{asset['asset_id']}/content")
        assert content.status_code == 200 and content.content == media_files["image"]
        assert content.headers["content-type"] == "image/png"
        assert content.headers["x-content-type-options"] == "nosniff"
        assert http.get("/v1/source-assets/asset_nope").status_code == 404


@needs_ffmpeg
def test_video_upload_extracts_keyframes(tmp_path, media_files):
    with TestClient(create_app(workers=0, media=MediaStore(tmp_path))) as http:
        asset = upload(http, media_files["video"], "clip.mp4").json()
        assert asset["kind"] == "video" and asset["has_audio"] is True
        assert 5900 <= asset["duration_ms"] <= 6100
        assert asset["transcript_status"] == "pending"
        # 6 s clip: one frame from the middle of each second.
        assert [f["t_ms"] for f in asset["frames"]] == [500, 1500, 2500, 3500, 4500, 5500]
        frame = http.get(f"/v1/source-assets/{asset['asset_id']}/frames/0")
        assert frame.status_code == 200 and frame.content[:3] == b"\xff\xd8\xff"
        assert http.get(f"/v1/source-assets/{asset['asset_id']}/frames/6").status_code == 404
        ranged = http.get(
            f"/v1/source-assets/{asset['asset_id']}/content", headers={"Range": "bytes=0-99"}
        )
        assert ranged.status_code == 206 and len(ranged.content) == 100

        silent = upload(http, media_files["silent"], "silent.mp4").json()
        assert silent["has_audio"] is False and silent["transcript_status"] == "no_audio"


@needs_ffmpeg
def test_bad_uploads_are_rejected_and_leave_nothing_behind(tmp_path, media_files):
    store = MediaStore(tmp_path, MediaLimits(max_image_bytes=1000))
    with TestClient(create_app(workers=0, media=store)) as http:
        r = upload(http, b"<html><script>alert(1)</script></html>", "x.png")
        assert r.status_code == 422 and "unsupported file type" in r.json()["detail"]
        assert upload(http, b"", "x.png").status_code == 422
        r = upload(http, media_files["image"], "big.png")
        assert r.status_code == 413
        broken = b"\0\0\0\x18ftypisom" + b"\0" * 2000  # looks like MP4, isn't one
        r = upload(http, broken, "broken.mp4")
        assert r.status_code == 422 and "could not be read" in r.json()["detail"]
    assert list(tmp_path.iterdir()) == []


@needs_ffmpeg
def test_job_with_image_and_prompt_runs_and_shows_the_model_the_image(tmp_path, media_files):
    client = FixtureModelClient()
    app = create_app(client=client, media=MediaStore(tmp_path), poll_s=0.01)
    with TestClient(app) as http:
        asset = upload(http, media_files["image"], "street.png").json()
        prompt = "make the person in the photo the lead in a revenge story"
        body = {
            "sources": [
                {"type": "image", "asset_id": asset["asset_id"], "text": prompt},
            ]
        }
        job_id = http.post("/v1/drama-jobs", json=body).json()["job_id"]
        job = wait_job(http, job_id)
        assert job["status"] == "completed"

        ingest = client.calls[0]
        assert [p.label for p in ingest.media] == [f"source[0] image ({asset['asset_id']})"]
        [entry] = ingest.context["source_media"]
        assert entry["user_prompt"] == prompt and entry["kind"] == "image"
        assert all(not c.media for c in client.calls[1:])  # later steps use the brief

        [summary] = http.get("/v1/drama-jobs").json()
        assert summary["seed"] == f"[image] street.png · {prompt}"
        kinds = [e["kind"] for e in http.get(f"/v1/drama-jobs/{job_id}/trace").json()["events"]]
        assert "media_attached" in kinds


@needs_ffmpeg
def test_job_rejects_a_mismatched_asset_type(tmp_path, media_files):
    with TestClient(create_app(workers=0, media=MediaStore(tmp_path))) as http:
        asset = upload(http, media_files["image"], "a.png").json()
        body = {"sources": [{"type": "video", "asset_id": asset["asset_id"]}]}
        r = http.post("/v1/drama-jobs", json=body)
        assert r.status_code == 422 and "is a image" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Transcription


class TranscribingFixture(FixtureModelClient):
    transcribe_model = "whisper-test"

    def __init__(self, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.transcribed: list[str] = []

    async def transcribe(self, audio_path):
        assert audio_path.stat().st_size > 0
        self.transcribed.append(audio_path)
        await asyncio.sleep(0.05)  # lets a concurrent job reach the lock meanwhile
        if self.fail:
            raise ConnectionError("audio endpoint down")
        return [
            {"start_ms": 0, "end_ms": 2000, "text": "Dad, is that really you?"},
            {"start_ms": 5000, "end_ms": 7000, "text": "Back for the rent."},  # ends after the clip
        ]


def _video_job(tmp_path, media_files, client):
    store = MediaStore(tmp_path)
    repo = InMemoryRepository()

    async def chunks():
        yield media_files["video"]

    asset = asyncio.run(store.save_upload(chunks(), "clip.mp4"))
    repo.save_source_asset(asset.asset_id, asset.model_dump(mode="json"))
    req = JobRequest.model_validate(
        {
            "sources": [
                {
                    "type": "video",
                    "asset_id": asset.asset_id,
                    "text": "keep the father-daughter reunion",
                }
            ]
        }
    )
    orch = Orchestrator(repo, client, media=store)
    return repo, orch, req, asset.asset_id


@needs_ffmpeg
def test_video_is_transcribed_once_and_the_transcript_reaches_ingest(tmp_path, media_files):
    client = TranscribingFixture()
    repo, orch, req, asset_id = _video_job(tmp_path, media_files, client)
    for n in (1, 2):
        repo.save_job(Job(job_id=f"job_{n}", request=req))
        assert asyncio.run(orch.run_job(f"job_{n}")).status == "completed"
    assert len(client.transcribed) == 1  # cached on the asset for the second job
    assert not client.transcribed[0].exists()  # the temporary audio file is removed

    asset = SourceAsset.model_validate(repo.get_source_asset(asset_id))
    assert asset.transcript_status == "done" and asset.transcript_model == "whisper-test"
    entry = client.calls[0].context["source_media"][0]
    duration = asset.duration_ms
    assert entry["transcript"] == [
        {"start_ms": 0, "end_ms": 2000, "text": "Dad, is that really you?"},
        {"start_ms": 5000, "end_ms": duration, "text": "Back for the rent."},  # clamped
    ]
    assert entry["audio"].startswith("speech transcribed in full")
    assert len(client.calls[0].media) == 6
    assert "frame at 00:00.5 (t=500 ms" in client.calls[0].media[0].label

    [call] = [c for c in repo.list_llm_calls("job_1") if c.api == "transcription"]
    assert call.status == "succeeded" and call.model == "whisper-test"
    assert "Dad, is that really you" in repo.get_llm_call(call.call_id).output


@needs_ffmpeg
def test_failed_transcription_marks_audio_unknown_and_the_job_goes_on(tmp_path, media_files):
    client = TranscribingFixture(fail=True)
    repo, orch, req, asset_id = _video_job(tmp_path, media_files, client)
    repo.save_job(Job(job_id="job_1", request=req))
    assert asyncio.run(orch.run_job("job_1")).status == "completed"
    asset = SourceAsset.model_validate(repo.get_source_asset(asset_id))
    assert asset.transcript_status == "failed" and "audio endpoint down" in asset.transcript_error
    entry = client.calls[0].context["source_media"][0]
    assert "transcript" not in entry and "not analyzed (unknown)" in entry["audio"]


def test_ingest_gate_rejects_video_coverage_outside_the_video():
    req = JobRequest.model_validate({"sources": [{"type": "video", "asset_id": "asset_v"}]})
    state = PipelineState(request=req, package_id="p", revision_id="r")
    state.source_assets = {
        "asset_v": SourceAsset(
            asset_id="asset_v",
            kind="video",
            filename="v.mp4",
            content_type="video/mp4",
            size_bytes=1,
            sha256="x",
            file="asset_v/original.mp4",
            duration_ms=10_000,
        )
    }
    brief = SourceBrief.model_validate(VALID_BRIEF)
    brief.source_coverage = [
        SourceCoverage(
            source_index=0,
            asset_id="asset_other",
            analyzed_ranges=[TimeRange(start_ms=0, end_ms=60_000)],
        )
    ]
    state.source_brief = brief
    ingest = next(s for s in STEPS if s.name == "ingest")
    evidence = [i.evidence for i in ingest.check(state)]
    assert any("should be asset_v" in e for e in evidence)
    assert any("outside the video" in e for e in evidence)

    brief.source_coverage[0].asset_id = "asset_v"
    brief.source_coverage[0].analyzed_ranges = [TimeRange(start_ms=0, end_ms=10_000)]
    assert ingest.check(state) == []


# ---------------------------------------------------------------------------
# MCPHub wire format


def test_responses_request_carries_labeled_images_but_the_trace_does_not(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8\xffJPEGDATA")
    rec = FakeRecorder()
    client, seen = make_client(lambda r, n: responses_sse(json.dumps(VALID_BRIEF)))
    req = request(recorder=rec, media=[MediaPart("source[0] image (asset_a)", img)])
    asyncio.run(client.generate(req, SourceBrief))

    content = json.loads(seen[0].content)["input"][0]["content"]
    assert content[0]["type"] == "input_text" and "## Context" in content[0]["text"]
    assert content[1] == {"type": "input_text", "text": "source[0] image (asset_a)"}
    assert content[2]["type"] == "input_image"
    assert content[2]["image_url"].startswith("data:image/jpeg;base64,/9j/")

    recorded = json.dumps(rec.calls[0]["request"])
    assert "base64" not in recorded and '"file": "f.jpg"' in recorded


def test_chat_request_uses_image_url_parts(tmp_path):
    from .test_mcphub_client import chat_sse

    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8\xffJPEGDATA")
    client, seen = make_client(lambda r, n: chat_sse(json.dumps(VALID_BRIEF)), api="chat")
    asyncio.run(client.generate(request(media=[MediaPart("img", img)]), SourceBrief))
    content = json.loads(seen[0].content)["messages"][1]["content"]
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_transcribe_posts_audio_and_converts_segments(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3fake")
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(
            200,
            json={
                "text": "Dad?",
                "segments": [
                    {"start": 0.0, "end": 1.25, "text": " Dad?"},
                    {"start": 1.25, "end": 2.0, "text": "  "},
                ],
            },
        )

    client = MCPHubClient(
        "https://hub.test/v1", "sk-test", "gpt-6.1-sol", backoff_s=0,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    segments = asyncio.run(client.transcribe(audio))
    assert segments == [{"start_ms": 0, "end_ms": 1250, "text": "Dad?"}]
    assert len(seen) == 2 and seen[1].url.path == "/v1/audio/transcriptions"
    body = seen[1].content
    assert b'name="model"\r\n\r\nwhisper-1' in body and b"ID3fake" in body


@needs_ffmpeg
def test_concurrent_jobs_sharing_a_video_transcribe_it_once(tmp_path, media_files):
    client = TranscribingFixture()
    repo, orch, req, _ = _video_job(tmp_path, media_files, client)
    for n in (1, 2):
        repo.save_job(Job(job_id=f"job_{n}", request=req))

    async def both():
        return await asyncio.gather(orch.run_job("job_1"), orch.run_job("job_2"))

    assert [j.status for j in asyncio.run(both())] == ["completed", "completed"]
    assert len(client.transcribed) == 1
    ingests = [c for c in client.calls if c.stage == "ingest"]
    assert all("transcript" in c.context["source_media"][0] for c in ingests)


def test_heif_photos_and_audio_files_are_not_taken_for_video(tmp_path):
    from drama_agent.media import MediaError

    store = MediaStore(tmp_path)

    def save(data: bytes):
        async def chunks():
            yield data

        return asyncio.run(store.save_upload(chunks(), "x"))

    assert sniff(b"\0\0\0\x18ftypheic") is None
    assert sniff(b"\0\0\0\x18ftypM4A ") is None
    with pytest.raises(MediaError, match="HEIC/AVIF photos aren't supported"):
        save(b"\0\0\0\x18ftypheic" + b"\0" * 100)
    with pytest.raises(MediaError, match="unsupported file type"):
        save(b"\0\0\0\x18ftypM4A " + b"\0" * 100)


def _frames(n: int) -> list:
    from drama_agent.models import VideoFrame

    return [VideoFrame(t_ms=i * 1000, file=f"v/frame_{i:03d}.jpg") for i in range(n)]


def test_images_per_request_are_capped_by_thinning_video_keyframes(tmp_path):
    from drama_agent.media import source_media

    store = MediaStore(tmp_path, MediaLimits(max_images_per_request=10))
    assets, sources = {}, []
    for k in range(3):
        a = SourceAsset(
            asset_id=f"asset_v{k}", kind="video", filename="v.mp4", content_type="video/mp4",
            size_bytes=1, sha256="x", file="v/original.mp4", duration_ms=24_000,
            frames=_frames(24), transcript_status="no_audio",
        )  # fmt: skip
        assets[a.asset_id] = a
        sources.append({"type": "video", "asset_id": a.asset_id})
    img = SourceAsset(
        asset_id="asset_i", kind="image", filename="i.png", content_type="image/png",
        size_bytes=1, sha256="x", file="i/original.png",
    )  # fmt: skip
    assets["asset_i"] = img
    sources.append({"type": "image", "asset_id": "asset_i"})

    ctx, parts = source_media(JobRequest.model_validate({"sources": sources}), assets, store)
    assert len(parts) == 10  # 1 image + 3 videos x 3 keyframes
    assert ctx[0]["keyframes_ms"] == [4000, 12000, 20000]  # spread over the whole video


def test_job_media_source_count_is_limited(tmp_path):
    store = MediaStore(tmp_path, MediaLimits(max_media_sources=2))
    with TestClient(create_app(workers=0, media=store)) as http:
        body = {"sources": [{"type": "image", "asset_id": f"asset_{i}"} for i in range(3)]}
        r = http.post("/v1/drama-jobs", json=body)
        assert r.status_code == 422 and "at most 2" in r.json()["detail"]


@needs_ffmpeg
def test_without_ffmpeg_only_small_images_are_accepted(tmp_path, media_files, monkeypatch):
    import drama_agent.media as media_mod

    monkeypatch.setattr(media_mod, "ffmpeg_available", lambda: False)
    store = MediaStore(tmp_path, MediaLimits(max_unscaled_image_bytes=1000))
    with TestClient(create_app(workers=0, media=store)) as http:
        r = upload(http, media_files["image"], "big.png")
        assert r.status_code == 413 and "can't downscale" in r.json()["detail"]
        r = upload(http, media_files["video"], "clip.mp4")
        assert r.status_code == 422 and "need ffmpeg" in r.json()["detail"]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_source_assets_batch_lookup(tmp_path, kind):
    from drama_agent.store.sqlite import SqliteRepository

    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    repo.save_source_asset("a1", {"filename": "one.png"})
    repo.save_source_asset("a2", {"filename": "two.mp4"})
    got = repo.get_source_assets(["a2", "missing", "a1", "a2"])
    assert got == {"a1": {"filename": "one.png"}, "a2": {"filename": "two.mp4"}}
    assert repo.get_source_assets([]) == {}
