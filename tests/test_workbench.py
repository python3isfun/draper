"""The workbench, phase 1 (docs/workbench.md): chats tied to a design picture, started from
its pick, and Use as pick."""

import time

from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.imagegen import FixtureImageClient
from drama_agent.images import outdated_pictures, selected_image
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore

from .conftest import wait_job
from .test_import_pick import BODY, FRANK_FACE


def _app(tmp_path):
    return create_app(
        client=FixtureModelClient(), image_client=FixtureImageClient(),
        media=MediaStore(tmp_path), poll_s=0.01,
    )  # fmt: skip


def _story(http) -> dict:
    job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
    base = f"/v1/drama-packages/{job['package_id']}/images"
    http.post(base, json={"job_id": job["job_id"]})  # draw every design picture
    deadline = time.monotonic() + 30
    while http.get(base).json()["queued"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return job


def _target(job) -> dict:
    return {"package_id": job["package_id"], "requirement_id": FRANK_FACE[0],
            "deliverable_id": FRANK_FACE[1], "label": "Frank Collins, face"}  # fmt: skip


def _wait_turn(http, sid):
    deadline = time.monotonic() + 10
    while (v := http.get(f"/v1/experiments/{sid}").json())["turns"][-1]["status"] != "done":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return v


def test_a_chat_for_a_design_picture_starts_from_its_pick(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        repo = app.state.repo
        pkg = repo.get_package(job["package_id"])
        pick = selected_image(repo.list_images(pkg.package_id, pkg.revision_id), FRANK_FACE)
        pick = pick.model_dump(mode="json")
        r = http.post("/v1/experiments", json={"target": _target(job)})
        assert r.status_code == 201, r.text
        s = r.json()
        assert (
            s["title"] == "Frank Collins, face" and s["target"]["requirement_id"] == FRANK_FACE[0]
        )
        v = http.get(f"/v1/experiments/{s['session_id']}").json()
        [first] = v["turns"]
        assert first["mode"] == "pick" and first["status"] == "done"
        assert first["prompt"] == pick["prompt"] and first["likeness"] == pick["likeness"]
        assert first["results"][0]["label"] == 1
        assert v["session"]["current"] == f"turn:{first['turn_id']}:0"
        # The copy is the pick's picture.
        got = http.get(f"/v1/experiment-turns/{first['turn_id']}/results/0/content")
        assert got.status_code == 200
        assert got.content == http.get(f"/v1/generated-images/{pick['image_id']}/content").content
        # The next message changes it, written from the pick's prompt.
        http.post(f"/v1/experiments/{s['session_id']}/turns", json={"text": "grey beard"})
        v = _wait_turn(http, s["session_id"])
        assert v["turns"][-1]["references"][0] == f"turn:{first['turn_id']}:0"


def test_a_slots_chats_are_listed_and_unknown_slots_refused(tmp_path):
    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        mine = http.post("/v1/experiments", json={"target": _target(job)}).json()
        http.post("/v1/experiments", json={})  # a free experiment
        q = {"package_id": job["package_id"], "requirement_id": FRANK_FACE[0],
             "deliverable_id": FRANK_FACE[1]}  # fmt: skip
        listed = http.get("/v1/experiments", params=q).json()
        assert [s["session_id"] for s in listed] == [mine["session_id"]]
        assert len(http.get("/v1/experiments").json()) == 2
        bad = {**_target(job), "deliverable_id": "d_nope"}
        assert http.post("/v1/experiments", json={"target": bad}).status_code == 404
        # A study (Face Lab) can be for a picture too; it doesn't start from the pick.
        study = http.post("/v1/experiments", json={"kind": "face", "target": _target(job)})
        assert study.status_code == 201
        assert http.get(f"/v1/experiments/{study.json()['session_id']}").json()["turns"] == []


def test_use_as_pick_makes_a_result_the_pick_and_what_was_drawn_from_it_out_of_date(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        sid = http.post("/v1/experiments", json={"target": _target(job)}).json()["session_id"]
        http.post(f"/v1/experiments/{sid}/turns", json={"text": "grey beard"})
        _wait_turn(http, sid)
        r = http.post(f"/v1/experiments/{sid}/use", json={"image": 2})
        assert r.status_code == 201, r.text
        img = r.json()
        assert img["selected"] and (img["requirement_id"], img["deliverable_id"]) == FRANK_FACE
        assert img["imported_from"] == f"experiment {sid} #2"
        # Frank's looks were drawn from the old face: now out of date.
        repo = app.state.repo
        pkg = repo.get_package(job["package_id"])
        stale = outdated_pictures(pkg, repo.list_images(pkg.package_id, pkg.revision_id))
        looks = [i for i in repo.list_images(pkg.package_id, pkg.revision_id)
                 if i.image_id in stale]  # fmt: skip
        assert looks and all(i.requirement_id.startswith("ar_character_look_ch_frank")
                             for i in looks)  # fmt: skip
        # A free experiment has no picture to be the pick of.
        free = http.post("/v1/experiments", json={}).json()["session_id"]
        assert http.post(f"/v1/experiments/{free}/use", json={"image": 1}).status_code == 409
        assert http.post(f"/v1/experiments/{sid}/use", json={"image": 99}).status_code == 404


def test_a_pick_from_a_story_with_uploads_starts_with_an_unknown_likeness(tmp_path, monkeypatch):
    import drama_agent.api.experiments as routes
    from drama_agent.experiment import picture_kind

    monkeypatch.setattr(routes, "package_has_uploads", lambda *_: True)
    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        sid = http.post("/v1/experiments", json={"target": _target(job)}).json()["session_id"]
        turns = app.state.repo.list_exp_turns(sid)
        assert turns[0].likeness is None  # judged from the picture, like an upload
        assert "story with uploaded photos" in picture_kind(f"turn:{turns[0].turn_id}:0", turns)


def test_a_pick_whose_file_is_gone_starts_an_empty_chat(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        repo = app.state.repo
        pkg = repo.get_package(job["package_id"])
        pick = selected_image(repo.list_images(pkg.package_id, pkg.revision_id), FRANK_FACE)
        (tmp_path / pick.file).unlink()
        r = http.post("/v1/experiments", json={"target": _target(job)})
        assert r.status_code == 201
        assert http.get(f"/v1/experiments/{r.json()['session_id']}").json()["turns"] == []


def test_a_scratch_chat_can_be_linked_to_a_picture_and_unlinked(tmp_path):
    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        sid = http.post("/v1/experiments", json={}).json()["session_id"]
        r = http.patch(f"/v1/experiments/{sid}", json={"target": _target(job)})
        assert r.status_code == 200 and r.json()["target"]["requirement_id"] == FRANK_FACE[0]
        q = {"package_id": job["package_id"], "requirement_id": FRANK_FACE[0]}
        assert [s["session_id"] for s in http.get("/v1/experiments", params=q).json()] == [sid]
        # Linking keeps the chat as it is: it doesn't start from the pick.
        assert http.get(f"/v1/experiments/{sid}").json()["turns"] == []
        bad = {**_target(job), "requirement_id": "ar_nope"}
        assert http.patch(f"/v1/experiments/{sid}", json={"target": bad}).status_code == 404
        r = http.patch(f"/v1/experiments/{sid}", json={"unlink": True})
        assert r.json()["target"] is None
        assert http.get("/v1/experiments", params=q).json() == []


def test_a_face_study_is_only_for_a_face(tmp_path):
    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        pkg = http.get(f"/v1/drama-packages/{job['package_id']}").json()
        look = next(r for r in pkg["asset_requirements"]["items"]
                    if r["asset_type"] == "character_look")  # fmt: skip
        target = {"package_id": job["package_id"], "requirement_id": look["requirement_id"],
                  "deliverable_id": look["deliverable_ids"][0] if "deliverable_ids" in look
                  else look["deliverables"][0]["deliverable_id"]}  # fmt: skip
        r = http.post("/v1/experiments", json={"kind": "face", "target": target})
        assert r.status_code == 422
        sid = http.post("/v1/experiments", json={"kind": "face"}).json()["session_id"]
        assert http.patch(f"/v1/experiments/{sid}", json={"target": target}).status_code == 422
        assert (
            http.patch(f"/v1/experiments/{sid}", json={"target": _target(job)}).status_code == 200
        )
