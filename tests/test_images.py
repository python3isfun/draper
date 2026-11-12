"""On-demand reference images: client, planning, references, worker, API."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import (
    FixtureImageClient,
    ImageGenError,
    MiniMaxImageClient,
    Reference,
    solid_png,
)
from drama_agent.images import ImageWorker, image_client_from_env, plan_images
from drama_agent.media import MediaStore
from drama_agent.models.images import GeneratedImage
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}


def b64_image(data: bytes = b"\xff\xd8\xffJPEG") -> dict:
    import base64

    return {"model": "minimax-h3-image", "data": [{"b64_json": base64.b64encode(data).decode()}]}


def minimax(handler) -> tuple[MiniMaxImageClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req, len(seen))

    client = MiniMaxImageClient(
        "mcp_test", base_url="https://hub.test/api/v3/gpu-images", backoff_s=0,
        transport=httpx.MockTransport(wrapped),
    )  # fmt: skip
    return client, seen


# ---------------------------------------------------------------------------
# Client


def test_text_to_image_request_shape():
    client, seen = minimax(lambda r, n: httpx.Response(200, json=b64_image(b"IMG")))
    result = asyncio.run(client.generate("a fox", "1024x1024", 7, []))
    assert result.data == b"IMG" and result.content_type == "image/jpeg"
    req = seen[0]
    assert req.url.path == "/api/v3/gpu-images/minimax-h3-image/generations"
    assert req.headers["x-api-key"] == "mcp_test" and "authorization" not in req.headers
    assert json.loads(req.content) == {
        "prompt": "a fox", "size": "1024x1024", "seed": 7, "output_format": "jpeg",
    }  # fmt: skip


def test_edit_sends_references_in_order_and_retries_gateway_errors():
    responses = [httpx.Response(502, text="restarting"), httpx.Response(200, json=b64_image())]
    client, seen = minimax(lambda r, n: responses[n - 1])
    refs = [Reference("face.jpg", b"FACE"), Reference("room.png", b"ROOM", "image/png")]
    asyncio.run(client.generate("<Picture 1> in <Picture 2>", "576x1024", 3, refs))
    assert len(seen) == 2 and seen[1].url.path.endswith("/edits")
    body = seen[1].content
    assert body.index(b"FACE") < body.index(b"ROOM")
    assert b'name="prompt"\r\n\r\n<Picture 1> in <Picture 2>' in body
    assert b'name="output_format"\r\n\r\njpeg' in body


def test_client_errors_are_not_retried_and_never_include_the_key():
    client, seen = minimax(lambda r, n: httpx.Response(403, text="not a newsbreak.com account"))
    with pytest.raises(ImageGenError, match="HTTP 403") as err:
        asyncio.run(client.generate("x", "1024x1024", 1, []))
    assert len(seen) == 1 and "mcp_test" not in str(err.value)
    with pytest.raises(ImageGenError, match="at most 9"):
        asyncio.run(client.generate("x", "1024x1024", 1, [Reference("r", b"x")] * 10))


def test_solid_png_is_a_valid_png():
    png = solid_png(3, 2, b"\x10\x20\x30")
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")


# ---------------------------------------------------------------------------
# Planning


def by_req(images: list[GeneratedImage]) -> dict[str, GeneratedImage]:
    return {f"{i.requirement_id}:{i.deliverable_id.rsplit('_', 1)[-1]}": i for i in images}


def test_plan_covers_cast_sets_props_in_dependency_order(package):
    plan = plan_images(package, [])
    kinds = [i.requirement_id.split("_")[1] for i in plan]
    assert set(kinds) == {"character", "location", "prop"}
    assert not any("shot" in i.requirement_id for i in plan)
    expected = sum(
        len([d for d in r.deliverables if d.required])
        for r in package.asset_requirements.items
        if r.asset_type in ("character_identity", "character_look", "location", "prop")
    )
    assert len(plan) == expected
    ids = [i.requirement_id for i in plan]
    first_look = next(n for n, r in enumerate(ids) if "character_look" in r)
    assert all("character_identity" in r for r in ids[:first_look])


def test_references_keep_people_places_and_props_consistent(package):
    p = by_req(plan_images(package, []))
    face = p["ar_character_identity_ch_frank:face"]
    look = p["ar_character_look_ch_frank_lk_frank_rain:full"]
    assert look.reference_ids == [face.image_id]
    assert look.brief["references"] == [
        {
            "picture": 1,
            "subject": "<Subject 1>",
            "role": "identity: face, age, build and identifying marks stay",
        }
    ]
    assert face.brief["task"] == "new image" and look.brief["task"] == "edit"

    day_wide = p["ar_location_loc_shop_ls_shop_day:wide"]
    night_wide = p["ar_location_loc_shop_ls_shop_night:wide"]
    night_plan = p["ar_location_loc_shop_ls_shop_night:layout"]
    assert day_wide.reference_ids == [] and night_wide.reference_ids == [day_wide.image_id]
    assert night_plan.reference_ids == [night_wide.image_id]

    props = [i for k, i in p.items() if k.startswith("ar_prop_prop_photo")]
    assert props[0].reference_ids == [] and props[1].reference_ids == [props[0].image_id]
    assert face.size == "3552x4736" and look.size == "3072x5472"  # official 16 MP sizes
    assert plan_images(package, [], draft=True)[0].size == "768x1024"


def test_templates_follow_the_h3_rules(package):
    from drama_agent.image_prompts import problems

    p = by_req(plan_images(package, []))
    for img in p.values():  # every fallback prompt passes the writer's own checks
        assert problems(img.prompt, len(img.reference_ids)) == [], img.prompt
        assert img.prompt_source == "pending"
    face = p["ar_character_identity_ch_frank:face"].prompt
    assert face.startswith("A studio casting photograph of Frank Collins")  # what it is, first
    assert face.endswith("The image contains no text, letters or numbers.")
    look = p["ar_character_look_ch_frank_lk_frank_rain:full"].prompt
    # MiniMax's edit form: define the source, target overview, relationship, changes.
    assert look.startswith("The person (Frank Collins) in <Picture 1> is <Subject 1>.")
    assert "The target image is a full-length costume photograph" in look
    assert (
        "<Subject 1> is partially preserved; the face, age and build and An old burn scar" in look
    )
    assert look.endswith("No other lettering is added.")
    night = p["ar_location_loc_shop_ls_shop_night:wide"].prompt
    assert "<Subject 1> is partially preserved; its layout, furniture" in night
    plan = p["ar_location_loc_shop_ls_shop_night:layout"].prompt
    assert "overhead plan-view photograph" in plan and "same coordinate frame" in plan
    assert not any("Edit <Picture" in i.prompt for i in p.values())  # no editing commands


def test_replanning_skips_existing_and_retries_failed(package):
    first = plan_images(package, [])
    assert plan_images(package, first) == []
    first[0].status = "failed"
    [again] = plan_images(package, first)
    assert again.deliverable_id == first[0].deliverable_id and again.version == 2
    assert again.seed != first[0].seed
    assert plan_images(package, [])[0].seed == first[0].seed  # v1 seeds are deterministic


def test_regenerate_one_uses_current_references(package):
    plan = plan_images(package, [])
    p = by_req(plan)
    look = p["ar_character_look_ch_frank_lk_frank_rain:full"]
    face = p["ar_character_identity_ch_frank:face"]
    face2 = face.model_copy(update={"image_id": "img_face2", "version": 2})
    [new] = plan_images(package, [*plan, face2], only=(look.requirement_id, look.deliverable_id))
    assert new.version == 2 and new.reference_ids == ["img_face2"]
    with pytest.raises(KeyError):
        plan_images(package, plan, only=("ar_nope", "d_nope"))


# ---------------------------------------------------------------------------
# Worker


def run_worker(repo, media, client) -> None:
    async def drain():
        worker = ImageWorker(repo, media, client)
        while await worker.run_once():
            pass

    asyncio.run(drain())


def test_worker_generates_in_order_with_references(package, tmp_path):
    repo, media, client = InMemoryRepository(), MediaStore(tmp_path), FixtureImageClient()
    for img in plan_images(package, []):
        repo.save_image(img)
    run_worker(repo, media, client)
    images = repo.list_images(package.package_id)
    assert all(i.status == "done" and i.model == "fixture-image-v1" for i in images)
    assert all(media.path(i.file).read_bytes().startswith(b"\x89PNG") for i in images)
    look_call = next(c for c in client.calls if "costume photograph" in c[0])
    assert len(look_call[3]) == 1  # the face, passed as a reference image
    assert all(i.prompt_source == "template" for i in images)  # no writer given


def test_an_image_whose_reference_failed_fails_too(package, tmp_path):
    repo, media = InMemoryRepository(), MediaStore(tmp_path)
    for img in plan_images(package, []):
        repo.save_image(img)

    class FailFaces(FixtureImageClient):
        async def generate(self, prompt, size, seed, references):
            if "casting photograph" in prompt:
                raise ImageGenError("image generation failed: HTTP 502: down")
            return await super().generate(prompt, size, seed, references)

    client = FailFaces()
    run_worker(repo, media, client)
    p = by_req(repo.list_images(package.package_id))
    face = p["ar_character_identity_ch_frank:face"]
    look = p["ar_character_look_ch_frank_lk_frank_rain:full"]
    assert face.status == "failed" and "502" in face.error
    # The look's prompt defines <Picture 1> as the face; without it, it isn't generated.
    assert look.status == "failed" and "generate it first" in look.error
    assert not any("costume photograph" in c[0] for c in client.calls)

    # Generate missing re-plans both; the new look references the new face.
    again = {
        i.requirement_id: i for i in plan_images(package, repo.list_images(package.package_id))
    }
    new_face = again["ar_character_identity_ch_frank"]
    assert again["ar_character_look_ch_frank_lk_frank_rain"].reference_ids == [new_face.image_id]


def test_a_timed_out_request_is_not_resent():
    def slow(req, n):
        raise httpx.ReadTimeout("read timed out", request=req)

    client, seen = minimax(slow)
    with pytest.raises(ImageGenError, match="timed out"):
        asyncio.run(client.generate("x", "1024x1024", 1, []))
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_claim_order_and_restart_requeue(kind, tmp_path, package):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    plan = plan_images(package, [])[:3]
    for img in plan:
        repo.save_image(img)
    first = repo.claim_next_image()
    assert first.image_id == plan[0].image_id and first.status == "running"
    assert repo.claim_next_image().image_id == plan[1].image_id
    # Another process may still be working on a young running image: only stale ones move.
    assert repo.requeue_running_images(older_than_s=1200) == 0
    assert repo.requeue_running_images() == 2
    assert repo.claim_next_image().image_id == plan[0].image_id
    assert [i.image_id for i in repo.list_images(package.package_id)] == [i.image_id for i in plan]
    assert repo.list_images(package.package_id, "rev_other") == []


# ---------------------------------------------------------------------------
# API and config


def wait_images(http, base, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        view = http.get(base).json()
        if not view["pending"] and not view["running"]:
            return view
        if time.monotonic() > deadline:
            raise AssertionError(f"images still busy: {view}")
        time.sleep(0.05)


def test_api_generate_list_regenerate_and_serve(tmp_path):
    app = create_app(media=MediaStore(tmp_path), poll_s=0.01)
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        base = f"/v1/drama-packages/{job['package_id']}/images"
        assert http.get(base).json()["images"] == []

        queued = http.post(base, params={"draft": True}).json()
        assert queued["pending"] + queued["running"] + queued["done"] > 10
        view = wait_images(http, base)
        assert view["failed"] == 0 and view["done"] == len(view["images"])
        assert all(i["size"].startswith(("768", "576", "1024")) for i in view["images"])
        assert http.post(base).json()["pending"] == 0  # nothing missing: nothing queued

        first = view["images"][0]
        content = http.get(f"/v1/generated-images/{first['image_id']}/content")
        assert content.status_code == 200 and content.content.startswith(b"\x89PNG")
        assert http.get(f"/v1/generated-images/{first['image_id']}/thumb").status_code == 200

        new = http.post(f"/v1/generated-images/{first['image_id']}/regenerate").json()
        assert new["version"] == 2 and new["size"] == first["size"]  # stays draft size
        view = wait_images(http, base)
        assert len(view["images"]) == len(queued["images"]) + 1 and view["done"] == len(
            queued["images"]
        )

        assert http.get("/v1/generated-images/img_nope").status_code == 404
        assert http.post("/v1/drama-packages/pkg_nope/images").status_code == 404


def test_image_client_follows_the_model_client(monkeypatch):
    monkeypatch.delenv("DRAMA_IMAGE_CLIENT", raising=False)
    monkeypatch.setenv("DRAMA_MODEL_CLIENT", "fixture")
    assert isinstance(image_client_from_env(), FixtureImageClient)
    monkeypatch.setenv("DRAMA_MODEL_CLIENT", "mcphub")
    monkeypatch.setenv("MCPHUB_KEY", "mcp_fake")
    assert isinstance(image_client_from_env(), MiniMaxImageClient)
    monkeypatch.setenv("DRAMA_IMAGE_CLIENT", "fixture")  # explicit override wins
    assert isinstance(image_client_from_env(), FixtureImageClient)


# ---------------------------------------------------------------------------
# Prompt writer


def test_writer_writes_the_whole_batch_in_one_call(package, tmp_path):
    from drama_agent.image_prompts import ImagePromptWriter
    from drama_agent.llm import FixtureModelClient

    repo, media, text = InMemoryRepository(), MediaStore(tmp_path), FixtureModelClient()
    repo.save_package(package, published=True)
    for img in plan_images(package, [], job_id="job_x"):
        repo.save_image(img)
    worker = ImageWorker(repo, media, FixtureImageClient(), writer=ImagePromptWriter(text, repo))

    async def drain():
        while await worker.run_once():
            pass

    asyncio.run(drain())
    images = repo.list_images(package.package_id)
    assert [c.stage for c in text.calls] == ["image_prompts"]  # one call for all of them
    assert all(i.prompt_source == "model" and i.prompt_model == "fixture-v1" for i in images)
    brief = text.calls[0].context["images"][0]
    assert {"task", "aspect", "references", "subject_facts", "template_prompt"} <= set(brief)
    from drama_agent.prompts import load_h3_image_examples, load_h3_image_rules

    instructions = text.calls[0].instructions  # the skill's shared rules, verbatim
    assert load_h3_image_rules() in instructions and load_h3_image_examples() in instructions
    kinds = [e.kind for e in repo.list_events("job_x")]
    assert kinds == ["image_prompts_started", "image_prompts_written"]
    [call] = repo.list_llm_calls("job_x")
    assert call.stage == "image_prompts" and call.status == "succeeded"


def test_unusable_or_failed_prompts_fall_back_to_the_template(package):
    from drama_agent.image_prompts import ImagePromptsOutput, ImagePromptWriter, problems

    no_text = "The image contains no text, letters or numbers."
    edit = (
        "The man in <Picture 1> is <Subject 1>. The target image is a costume photograph. "
        "<Subject 1> is partially preserved. No other lettering is added."
    )
    assert problems(f"A portrait. {no_text}", 0) == []
    assert problems('A storefront; a sign reads "OPEN".', 0) == []  # quoted text instead
    assert problems("A man's portrait.", 0) == [
        f"does not say '{no_text}' or quote the text to show"
    ]  # an apostrophe is not a quote
    assert problems(edit, 1) == []
    assert problems(edit.replace("<Picture 1>", "<Picture 2>"), 1) == [
        "cites a picture it doesn't have (has 1)",
        "doesn't define <Picture 1> as a <Subject N>",
    ]
    assert 'has no "The target image ..." overview' in problems(
        edit.replace("The target image", "It"), 1
    )

    plan = plan_images(package, [])
    look = next(i for i in plan if "character_look" in i.requirement_id)
    face = next(i for i in plan if "character_identity" in i.requirement_id)

    class Writes:
        async def generate(self, request, output_model):
            request.model = "gpt-test"
            return ImagePromptsOutput.model_validate(
                {"prompts": [
                    {"image_id": face.image_id, "prompt": "A studio casting photograph of "
                     "him. The image contains no text, letters or numbers."},
                    {"image_id": look.image_id, "prompt": "Edit <Picture 3>. Wet."},
                ]}
            )  # fmt: skip

    asyncio.run(ImagePromptWriter(Writes(), InMemoryRepository()).write(package, [face, look]))
    assert face.prompt_source == "model" and face.prompt_model == "gpt-test"
    assert look.prompt_source == "template" and "cites a picture" in look.error
    assert look.prompt.startswith("The person (") and "<Subject 1>" in look.prompt

    class Fails:
        async def generate(self, request, output_model):
            raise ConnectionError("gateway down")

    again = plan_images(package, [])[:2]
    asyncio.run(ImagePromptWriter(Fails(), InMemoryRepository()).write(package, again))
    assert all(i.prompt_source == "template" and "writer failed" in i.error for i in again)


def test_regenerate_keeps_the_written_prompt(package):
    plan = plan_images(package, [])
    face = plan[0]
    face.prompt, face.prompt_source, face.status = "A studio casting photograph.", "model", "done"
    [new] = plan_images(package, plan, only=(face.requirement_id, face.deliverable_id))
    assert new.prompt == face.prompt and new.prompt_source == "model" and new.seed != face.seed


def test_skill_and_app_share_one_rules_file():
    """The minimax-h3-image-prompt skill points at the files the app's writer sends."""
    import re
    from pathlib import Path

    skill = Path(__file__).parent.parent / ".claude/skills/minimax-h3-image-prompt/SKILL.md"
    links = re.findall(r"\]\((\.\./[^)]+)\)", skill.read_text())
    targets = {(skill.parent / link).resolve() for link in links}
    pkg = Path(__file__).parent.parent / "src/drama_agent/prompts"
    assert {
        (pkg / "h3_image_rules.md").resolve(),
        (pkg / "h3_image_examples.md").resolve(),
    } <= targets
    assert all(t.is_file() for t in targets)


def test_generate_checks_the_job_and_refuses_archived_stories(tmp_path):
    app = create_app(media=MediaStore(tmp_path), poll_s=0.01)
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        base = f"/v1/drama-packages/{job['package_id']}/images"
        assert http.post(base, params={"job_id": "job_nope"}).status_code == 422
        ok = http.post(base, params={"job_id": job["job_id"], "draft": True}).json()
        assert all(i["job_id"] == job["job_id"] for i in ok["images"])

        stored = app.state.repo.get_job(job["job_id"])
        stored.archived = True
        app.state.repo.save_job(stored)
        assert http.post(base).status_code == 409
        first = ok["images"][0]["image_id"]
        assert http.post(f"/v1/generated-images/{first}/regenerate").status_code == 409
