"""Design exploration: takes x draws per anchor, selection, finalize, develop, export."""

import io
import time
import zipfile

from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import FixtureImageClient
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
FRANK_FACE = ("ar_character_identity_ch_frank", "d_ar_character_identity_ch_frank_face")
FRANK_RAIN = (
    "ar_character_look_ch_frank_lk_frank_rain",
    "d_ar_character_look_ch_frank_lk_frank_rain_full",
)


def key(x: dict) -> tuple[str, str]:
    return (x["requirement_id"], x["deliverable_id"])


class Session:
    def __init__(self, tmp_path):
        self.text = FixtureModelClient()
        self.images = FixtureImageClient()
        self.app = create_app(
            client=self.text, image_client=self.images, media=MediaStore(tmp_path), poll_s=0.01
        )

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        self.job = wait_job(self.http, self.http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        self.base = f"/v1/drama-packages/{self.job['package_id']}/images"
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def ids(self) -> dict:
        return {"job_id": self.job["job_id"]}

    def view(self) -> dict:
        return self.http.get(self.base).json()

    def wait(self, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            v = self.view()
            if not v["queued"]:
                return v
            assert time.monotonic() < deadline, f"{v['queued']} still queued"
            time.sleep(0.05)


def test_explore_writes_all_takes_in_one_call_and_draws_each(tmp_path):
    with Session(tmp_path) as s:
        r = s.http.post(f"{s.base}/explore", json={**s.ids(), "takes": 4, "draws": 3})
        assert r.status_code == 202
        v = s.wait()
        anchors = [x for x in v["slots"] if x["anchor"]]
        assert len(anchors) == 7  # 4 on-screen faces, 2 locations' first states, 1 prop
        assert len(v["options"]) == 7 * 4 and len(v["images"]) == 7 * 4 * 3
        assert all(i["status"] == "done" for i in v["images"])
        assert {i["size"] for i in v["images"]} <= {"768x1024", "1024x768", "1024x1024"}  # draft
        assert all(
            o["status"] == "written" and o["label"].startswith("Take ") for o in v["options"]
        )
        writer_calls = [c for c in s.text.calls if c.stage == "image_prompts"]
        assert len(writer_calls) == 1  # every take of the exploration in one call
        groups = writer_calls[0].context["groups"]
        assert len(groups) == 7 and {g["takes"] for g in groups} == {4}
        frank = [o for o in v["options"] if key(o) == FRANK_FACE]
        assert [o["index"] for o in frank] == [1, 2, 3, 4]
        draws = [i for i in v["images"] if i["option_id"] == frank[0]["option_id"]]
        assert sorted(i["draw"] for i in draws) == [1, 2, 3]
        assert len({i["seed"] for i in draws}) == 3


def test_picking_a_draft_renders_it_full_size_and_moves_the_selection(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 2, "draws": 2, "scope": ["cast"]}
        )
        v = s.wait()
        draft = next(i for i in v["images"] if key(i) == FRANK_FACE)
        pending = next(i for i in v["images"] if key(i) != FRANK_FACE)
        r = s.http.post(f"/v1/generated-images/{draft['image_id']}/select")
        assert r.status_code == 202 and r.json()["selected"] is True
        v = s.wait()
        frank = [i for i in v["images"] if key(i) == FRANK_FACE]
        final = next(i for i in frank if i["finalizes"] == draft["image_id"])
        assert final["size"] == "3552x4736" and final["seed"] == draft["seed"]
        # An edit of the picked draft (keeps the face), not a re-render of its prompt.
        assert final["status"] == "done" and final["reference_ids"] == [draft["image_id"]]
        assert final["prompt"].startswith(
            "The front face photograph in <Picture 1> is <Subject 1>."
        )
        assert "<Subject 1> is fully preserved." in final["prompt"]
        call = next(c for c in s.images.calls if c[1] == "3552x4736")
        assert call[3] == [f"{draft['image_id']}.png"]  # the draft was sent as the reference
        assert [i["image_id"] for i in frank if i["selected"]] == [final["image_id"]]
        # Selecting again doesn't render it twice; only one image per deliverable selected.
        other = next(
            i for i in frank if i["image_id"] not in (draft["image_id"], final["image_id"])
        )
        s.http.post(f"/v1/generated-images/{other['image_id']}/select")
        v = s.wait()
        frank = [i for i in v["images"] if key(i) == FRANK_FACE]
        assert len([i for i in frank if i["selected"]]) == 1
        assert s.http.post(f"/v1/generated-images/{pending['image_id']}/select").status_code == 202


def test_develop_waits_for_picks_and_draws_from_the_selected_face(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 2, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        r = s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 2})
        assert any("Frank Collins" in n for n in r.json()["notes"])  # nothing picked yet
        assert not any(o for o in r.json()["options"] if key(o) == FRANK_RAIN)

        take2 = next(o for o in v["options"] if key(o) == FRANK_FACE and o["index"] == 2)
        pick = next(i for i in v["images"] if i["option_id"] == take2["option_id"])
        s.http.post(f"/v1/generated-images/{pick['image_id']}/select")
        s.wait()
        s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 2})
        v = s.wait()
        [rain] = [o for o in v["options"] if key(o) == FRANK_RAIN]
        assert rain["brief"]["selected_design"]["label"] == "Take 2"
        rain_imgs = [i for i in v["images"] if i["option_id"] == rain["option_id"]]
        assert len(rain_imgs) == 2 and all(i["status"] == "done" for i in rain_imgs)
        assert rain_imgs[0]["reference_ids"] == [f"sel:{FRANK_FACE[0]}|{FRANK_FACE[1]}"]
        # The face reference sent to the image model is the selected (full-size) face.
        rain_calls = [c for c in s.images.calls if "costume photograph" in c[0]]
        final = next(i for i in v["images"] if i["finalizes"] == pick["image_id"])
        assert rain_calls and all(c[3] == [f"{final['image_id']}.png"] for c in rain_calls)

        # Develop again: deliverables that already have a take aren't duplicated.
        before = len(v["options"])
        s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 2})
        assert len(s.wait()["options"]) == before


def test_new_take_with_direction_and_more_draws(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 2, "draws": 1, "scope": ["cast"]}
        )
        s.wait()
        r = s.http.post(
            f"{s.base}/takes",
            json={
                **s.ids(),
                "requirement_id": FRANK_FACE[0],
                "deliverable_id": FRANK_FACE[1],
                "direction": "older, kinder eyes",
                "draws": 2,
            },
        )
        [opt] = r.json()
        assert opt["index"] == 3 and opt["direction"] == "older, kinder eyes"
        v = s.wait()
        call = [c for c in s.text.calls if c.stage == "image_prompts"][-1]
        [group] = call.context["groups"]
        assert group["direction"] == "older, kinder eyes" and len(group["avoid"]) == 2

        r = s.http.post(f"/v1/image-options/{opt['option_id']}/draws", json={"draws": 2})
        assert [i["draw"] for i in r.json()] == [3, 4]
        v = s.wait()
        assert len([i for i in v["images"] if i["option_id"] == opt["option_id"]]) == 4
        assert s.http.post("/v1/image-options/opt_nope/draws", json={}).status_code == 404
        bad = {**s.ids(), "requirement_id": "ar_nope", "deliverable_id": "d_nope"}
        assert s.http.post(f"{s.base}/takes", json=bad).status_code == 404


def test_export_has_the_selected_designs_and_every_other_option(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 2, "draws": 2, "scope": ["cast"]}
        )
        v = s.wait()
        pick = next(i for i in v["images"] if key(i) == FRANK_FACE)
        s.http.post(f"/v1/generated-images/{pick['image_id']}/select")
        v = s.wait()
        r = s.http.get(
            f"/v1/drama-packages/{s.job['package_id']}/export", params={"job_id": s.job["job_id"]}
        )
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = z.namelist()[0].split("/")[0]
    names = z.namelist()
    chosen = [n for n in names if "/reference_images/" in n and n.endswith(".png")]
    options = [n for n in names if "/05_assets/options/" in n and n.endswith(".png")]
    done = [i for i in v["images"] if i["status"] == "done"]
    assert len(chosen) == 4  # one per character face
    assert len(chosen) + len(options) == len(done)
    assert any(
        n.endswith("ar_character_identity_ch_frank__face_take1_draw1_full.png") for n in chosen
    )
    selected = z.read(f"{root}/04_design/selected_designs.md").decode()
    assert "## Frank Collins, front face" in selected and "(selected)" in selected
    assert "options.csv" in " ".join(names)


def test_unusable_takes_fall_back_to_the_template(package):
    import asyncio

    from drama_agent.image_prompts import ImageTakesOutput, write_takes
    from drama_agent.images import explore
    from drama_agent.store import InMemoryRepository

    opts, _ = explore(package, [], takes=2, draws=1, draft=True, scope=["props"], job_id=None)

    class Writes:
        async def generate(self, request, output_model):
            request.model = "gpt-test"
            [g] = request.context["groups"]
            return ImageTakesOutput.model_validate(
                {"groups": [{"group_id": g["group_id"], "takes": [
                    {"label": "Pristine heirloom", "profile": "Glossy, barely faded.",
                     "prompt": "A product photograph of the photo. The image contains no "
                               "text, letters or numbers."},
                    {"label": "Bad", "prompt": "Edit <Picture 2>."},
                ]}]}
            )  # fmt: skip

    asyncio.run(write_takes(Writes(), InMemoryRepository(), package, opts))
    good, bad = opts
    assert good.status == "written" and good.label == "Pristine heirloom"
    assert good.prompt_model == "gpt-test" and good.profile == "Glossy, barely faded."
    assert bad.status == "template" and "cites a picture" in bad.error and bad.label == "Take 2"


def test_edited_prompt_draws_a_new_take_with_the_same_seeds(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 2, "draws": 3, "scope": ["cast"]}
        )
        v = s.wait()
        take1 = next(o for o in v["options"] if key(o) == FRANK_FACE and o["index"] == 1)
        src = sorted(
            (i for i in v["images"] if i["option_id"] == take1["option_id"]),
            key=lambda i: i["draw"],
        )
        edited = take1["prompt"].replace("A studio casting photograph", "A candid photograph")
        assert edited != take1["prompt"]
        r = s.http.post(f"/v1/image-options/{take1['option_id']}/edit", json={"prompt": edited})
        assert r.status_code == 202, r.text
        opt = r.json()
        assert opt["status"] == "edited" and opt["edited_from"] == take1["option_id"]
        assert opt["index"] == 3 and opt["group_id"] == take1["group_id"]
        assert opt["prompt"] == edited and opt["warnings"] == []
        v = s.wait()
        mine = sorted(
            (i for i in v["images"] if i["option_id"] == opt["option_id"]),
            key=lambda i: i["draw"],
        )
        assert [i["seed"] for i in mine] == [i["seed"] for i in src]  # draw for draw
        assert all(i["status"] == "done" and i["prompt_source"] == "user" for i in mine)
        assert all(i["size"] == src[0]["size"] for i in mine)
        sent = [c for c in s.images.calls if c[0] == edited]
        assert len(sent) == 3
        # No writer call for an edited take: the user's words go to the image model as is.
        assert len([c for c in s.text.calls if c.stage == "image_prompts"]) == 1

        # More draws than the source has: the extra ones get new seeds.
        r = s.http.post(
            f"/v1/image-options/{take1['option_id']}/edit",
            json={"prompt": edited + " Soft window light from screen-left.", "draws": 4},
        )
        v = s.wait()
        extra = [i for i in v["images"] if i["option_id"] == r.json()["option_id"]]
        assert len(extra) == 4 and {i["seed"] for i in src} < {i["seed"] for i in extra}

        # Picking an edited draw and exporting: marked as edited in the hand-off.
        pick = mine[0]
        s.http.post(f"/v1/generated-images/{pick['image_id']}/select")
        s.wait()
        z = zipfile.ZipFile(
            io.BytesIO(
                s.http.get(
                    f"/v1/drama-packages/{s.job['package_id']}/export",
                    params={"job_id": s.job["job_id"]},
                ).content
            )
        )
    root = z.namelist()[0].split("/")[0]
    selected = z.read(f"{root}/04_design/selected_designs.md").decode()
    assert "Prompt edited by hand from take 1" in selected and "A candid photograph" in selected
    options_csv = z.read(f"{root}/05_assets/options/options.csv").decode()
    assert "edited_from" in options_csv.splitlines()[0] and "take 1" in options_csv


def test_edited_prompt_checks(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        s.wait()
        s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 1})  # waits: no pick
        v = s.view()
        face = next(o for o in v["options"] if key(o) == FRANK_FACE)
        url = f"/v1/image-options/{face['option_id']}/edit"
        # A face has no reference pictures: citing one would break the render.
        r = s.http.post(url, json={"prompt": "<Picture 1> is <Subject 1>. The target image."})
        assert r.status_code == 422 and "cites a picture" in r.json()["detail"]
        assert s.http.post(url, json={"prompt": "   "}).status_code == 422
        assert s.http.post(url, json={"prompt": "x" * 7000}).status_code == 422
        assert (
            s.http.post("/v1/image-options/opt_nope/edit", json={"prompt": "x"}).status_code == 404
        )
        # Rule misses that don't break the render are allowed and recorded.
        r = s.http.post(url, json={"prompt": "A casting photograph of Frank Collins."})
        assert r.status_code == 202
        assert any("contains no text" in w for w in r.json()["warnings"])


def test_more_draws_ignore_full_size_renders_for_references(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        draft = next(i for i in v["images"] if key(i) == FRANK_FACE)
        s.http.post(f"/v1/generated-images/{draft['image_id']}/select")
        s.wait()
        r = s.http.post(f"/v1/image-options/{draft['option_id']}/draws", json={"draws": 1})
        [img] = r.json()
        assert img["reference_ids"] == [] and img["draw"] == 2


def _fail(s, image_id):
    """Mark an image failed, as a provider error would."""
    repo = s.app.state.repo
    img = repo.get_image(image_id)
    img.status, img.error = "failed", "HTTP 503"
    repo.save_image(img)


def test_a_failed_full_size_render_is_retried_and_a_done_one_is_reused(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 2, "scope": ["props"]}
        )
        v = s.wait()
        a, b = sorted((i for i in v["images"]), key=lambda i: i["draw"])
        s.http.post(f"/v1/generated-images/{a['image_id']}/select")
        v = s.wait()
        [final] = [i for i in v["images"] if i["finalizes"] == a["image_id"]]
        _fail(s, final["image_id"])
        # Picking the draft again queues a new full-size render (the failed one isn't reused).
        s.http.post(f"/v1/generated-images/{a['image_id']}/select")
        v = s.wait()
        finals = [i for i in v["images"] if i["finalizes"] == a["image_id"]]
        assert len(finals) == 2 and [i["status"] for i in finals].count("done") == 1
        done_final = next(i for i in finals if i["status"] == "done")
        assert [i["image_id"] for i in v["images"] if i["selected"]] == [done_final["image_id"]]

        # A, then B, then A again: A's finished full-size render is the pick, not the draft.
        s.http.post(f"/v1/generated-images/{b['image_id']}/select")
        s.wait()
        r = s.http.post(f"/v1/generated-images/{a['image_id']}/select")
        assert r.json()["image_id"] == done_final["image_id"]
        v = s.wait()
        assert [i["image_id"] for i in v["images"] if i["selected"]] == [done_final["image_id"]]
        assert len([i for i in v["images"] if i["finalizes"] == a["image_id"]]) == 2  # no 3rd


def test_failed_draw_retry_keeps_its_seed(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["props"]}
        )
        [img] = s.wait()["images"]
        assert s.http.post(f"/v1/generated-images/{img['image_id']}/retry").status_code == 409
        _fail(s, img["image_id"])
        r = s.http.post(f"/v1/generated-images/{img['image_id']}/retry")
        assert r.status_code == 202 and r.json()["status"] == "pending"
        [again] = s.wait()["images"]
        assert again["status"] == "done" and again["seed"] == img["seed"] and not again["error"]


def test_the_full_size_render_only_takes_over_a_pick_that_is_still_there(package, tmp_path):
    from drama_agent.images import explore
    from drama_agent.store import InMemoryRepository
    from drama_agent.store.sqlite import SqliteRepository

    pkg = package
    _, imgs = explore(pkg, [], takes=1, draws=2, draft=True, scope=["props"], job_id=None)
    for repo in (InMemoryRepository(), SqliteRepository(tmp_path / "t.db")):
        a, b = (i.model_copy() for i in imgs[:2])
        for i in (a, b):
            i.status = "done"
            repo.save_image(i)
        assert repo.set_selected(a.image_id)
        assert repo.set_selected(b.image_id)  # the user moved on to B ...
        assert not repo.move_selection(
            a.image_id, "img_final"
        )  # ... so A's render doesn't take over
        got = {i.image_id: i.selected for i in repo.list_images(pkg.package_id)}
        assert got == {a.image_id: False, b.image_id: True}


def test_latest_images_is_the_pick(package):
    from drama_agent.images import explore, latest_images, plan_images

    _, imgs = explore(package, [], takes=2, draws=2, draft=True, scope=["cast"], job_id=None)
    face = [i for i in imgs if key(i.model_dump()) == FRANK_FACE]
    for i in imgs:
        i.status = "done"
    face[2].selected = True
    assert latest_images(imgs)[FRANK_FACE].image_id == face[2].image_id
    # Regenerating a look draws it from the picked face; nothing is "missing" to generate.
    [look] = plan_images(package, imgs, only=FRANK_RAIN)
    assert look.reference_ids == [face[2].image_id] and look.version == 1
    assert not [i for i in plan_images(package, imgs) if key(i.model_dump()) == FRANK_FACE]


def test_new_take_on_a_dependent_needs_its_parent_picked(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        body = {**s.ids(), "requirement_id": FRANK_RAIN[0], "deliverable_id": FRANK_RAIN[1]}
        r = s.http.post(f"{s.base}/takes", json=body)
        assert r.status_code == 409 and "Frank Collins, front face" in r.json()["detail"]
        face = next(i for i in v["images"] if key(i) == FRANK_FACE)
        s.http.post(f"/v1/generated-images/{face['image_id']}/select")
        s.wait()
        assert s.http.post(f"{s.base}/takes", json=body).status_code == 202


def test_a_location_state_plan_waits_for_that_states_pick(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["sets"]}
        )
        v = s.wait()
        slots = {(x["requirement_id"], x["deliverable_id"]): x for x in v["slots"]}
        night = next(k for k in slots if "shop_night" in k[0] and k[1].endswith("_wide"))
        plan = next(k for k in slots if "shop_night" in k[0] and k[1].endswith("_layout"))
        assert tuple(slots[plan]["parent_key"]) == night
        day = tuple(slots[night]["parent_key"])
        assert slots[day]["anchor"] and tuple(slots[plan]["anchor_key"]) == day
        pick = next(i for i in v["images"] if key(i) == day)
        s.http.post(f"/v1/generated-images/{pick['image_id']}/select")
        s.wait()
        r = s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 1})
        made = {key(o) for o in r.json()["options"]}
        assert night in made and plan not in made  # the plan waits for the night pick
        assert any("night" in n.lower() or "wide" in n for n in r.json()["notes"])
        v = s.wait()
        night_img = next(i for i in v["images"] if key(i) == night)
        s.http.post(f"/v1/generated-images/{night_img['image_id']}/select")
        s.wait()
        s.http.post(f"{s.base}/develop", json={**s.ids(), "draws": 1})
        v = s.wait()
        [plan_img] = [i for i in v["images"] if key(i) == plan]
        assert plan_img["reference_ids"] == [f"sel:{night[0]}|{night[1]}"]


def test_the_legacy_writer_leaves_take_draws_alone(package, tmp_path):
    import asyncio

    from drama_agent.images import ImageWorker, explore, plan_images
    from drama_agent.store import InMemoryRepository

    repo = InMemoryRepository()
    repo.save_package(package, True)
    legacy = plan_images(package, [])[:2]
    _, takes = explore(package, [], takes=1, draws=2, draft=True, scope=["props"], job_id=None)
    for img in [*legacy, *takes]:
        repo.save_image(img)
    batches = []

    class Writer:
        async def write(self, pkg, batch):
            batches.append({i.image_id for i in batch})

    worker = ImageWorker(repo, MediaStore(tmp_path), FixtureImageClient(), writer=Writer())
    asyncio.run(worker._write_prompts(legacy[0]))
    assert batches == [{i.image_id for i in legacy}]  # no take draws in the batch
