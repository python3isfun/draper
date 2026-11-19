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
        # Drawn at the pick's shape (a 3:4 face), not square.
        from drama_agent.experiment import size_of

        assert first["size"] == size_of(pick["size"]) != "2048x2048"
        assert v["turns"][-1]["size"] == first["size"]


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


def test_each_chat_for_a_picture_is_kept_and_an_untouched_one_reused(tmp_path):
    """Develop clicked again reopens the untouched chat; New chat after a message starts
    another, and both stay listed with their message counts and their own titles."""
    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        body = {"target": _target(job), "reuse_empty": True}
        first = http.post("/v1/experiments", json=body).json()
        assert http.post("/v1/experiments", json=body).json()["session_id"] == first["session_id"]
        # Without reuse_empty a new chat is always made.
        other = http.post("/v1/experiments", json={"target": _target(job)}).json()
        assert other["session_id"] != first["session_id"]
        # Once it has a message it is no longer untouched; it's titled by that message.
        sid = first["session_id"]
        http.post(f"/v1/experiments/{sid}/turns", json={"text": "grey beard"})
        _wait_turn(http, sid)
        again = http.post("/v1/experiments", json=body).json()
        assert again["session_id"] != sid
        q = {"package_id": job["package_id"], "requirement_id": FRANK_FACE[0],
             "deliverable_id": FRANK_FACE[1], "kind": "chat"}  # fmt: skip
        listed = {s["session_id"]: s for s in http.get("/v1/experiments", params=q).json()}
        assert listed[sid]["messages"] == 1 and listed[sid]["title"] == "grey beard"
        assert listed[other["session_id"]]["messages"] == 0
        assert listed[other["session_id"]]["title"] == "Frank Collins, face"
        # A renamed chat keeps its name past the first message.
        http.patch(f"/v1/experiments/{again['session_id']}", json={"title": "Beard ideas"})
        http.post(f"/v1/experiments/{again['session_id']}/turns", json={"text": "a short beard"})
        _wait_turn(http, again["session_id"])
        s = http.get(f"/v1/experiments/{again['session_id']}").json()["session"]
        assert s["title"] == "Beard ideas"


def test_a_new_pick_means_a_new_chat(tmp_path):
    """An untouched chat started from an earlier pick is not reused: it starts from the
    picture as it was."""
    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        body = {"target": _target(job), "reuse_empty": True}
        first = http.post("/v1/experiments", json=body).json()
        repo = app.state.repo
        [turn] = repo.list_exp_turns(first["session_id"])
        turn.from_image = "img_an_earlier_pick"
        repo.save_exp_turn(turn)
        assert http.post("/v1/experiments", json=body).json()["session_id"] != first["session_id"]


def test_a_pick_start_saved_without_a_size_gets_its_pictures(tmp_path):
    """Chats started before pick starts had a size drew square: their next message takes
    the picture's shape."""
    from drama_agent.experiment import size_of

    app = _app(tmp_path)
    with TestClient(app) as http:
        job = _story(http)
        repo = app.state.repo
        pkg = repo.get_package(job["package_id"])
        pick = selected_image(repo.list_images(pkg.package_id, pkg.revision_id), FRANK_FACE)
        sid = http.post("/v1/experiments", json={"target": _target(job)}).json()["session_id"]
        [first] = repo.list_exp_turns(sid)
        first.size, first.from_image = "", None  # as saved before either existed
        repo.save_exp_turn(first)
        http.post(f"/v1/experiments/{sid}/turns", json={"text": "grey beard"})
        v = _wait_turn(http, sid)
        assert v["turns"][0]["size"] == v["turns"][-1]["size"] == size_of(pick.size)


def test_create_from_text_reuses_the_faces_empty_study(tmp_path):
    """ "Create from text" clicked again opens the same empty face study; once it has a
    test, another is made."""
    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        body = {"kind": "face", "target": _target(job), "reuse_empty": True}
        first = http.post("/v1/experiments", json=body).json()
        assert first["kind"] == "face"
        assert http.post("/v1/experiments", json=body).json()["session_id"] == first["session_id"]
        # An empty chat for the same face is not a study.
        chat = http.post("/v1/experiments", json={"target": _target(job), "reuse_empty": True})
        assert chat.json()["session_id"] != first["session_id"]
        sid = first["session_id"]
        prompt = "A studio casting photograph of a man. The image contains no text."
        assert http.post(f"/v1/experiments/{sid}/tests", json={"prompt": prompt}).status_code == 202
        _wait_turn(http, sid)
        assert http.post("/v1/experiments", json=body).json()["session_id"] != sid


def test_a_chat_follows_its_storys_rating(tmp_path):
    """Images follow the story's MPA rating (content_guard.py): nudity only at R, and the
    rating can be changed after the story is made; a retry checks again at the new one."""
    from drama_agent.content_guard import nudity_reason

    with TestClient(_app(tmp_path)) as http:
        job = _story(http)
        pid = job["package_id"]
        sid = http.post("/v1/experiments", json={"target": _target(job)}).json()["session_id"]
        msg = {"text": "A naked man asleep under a white sheet, morning light.", "mode": "exact"}
        http.post(f"/v1/experiments/{sid}/turns", json=msg)
        turn = _wait_done_or_failed(http, sid)
        assert turn["rating"] == "PG-13" and turn["refused"]
        assert turn["error"] == nudity_reason("PG-13")

        assert http.get(f"/v1/drama-packages/{pid}/rating").json() == {"rating": "PG-13"}
        r = http.put(f"/v1/drama-packages/{pid}/rating", json={"rating": "R"})
        assert r.status_code == 200 and r.json() == {"rating": "R"}
        assert http.get(f"/v1/drama-packages/{pid}/rating").json() == {"rating": "R"}
        # Kept apart from the job, which a running pipeline saves from its own copy.
        job_now = http.get(f"/v1/drama-jobs/{job['job_id']}").json()
        assert job_now["request"]["options"]["content_rating"] == "PG-13"
        assert http.post(f"/v1/experiment-turns/{turn['turn_id']}/retry").status_code == 202
        turn = _wait_done_or_failed(http, sid)
        assert turn["rating"] == "R" and turn["status"] == "done" and turn["results"]

        assert (
            http.put(f"/v1/drama-packages/{pid}/rating", json={"rating": "NC-17"}).status_code
            == 422
        )
        assert http.put("/v1/drama-packages/nope/rating", json={"rating": "R"}).status_code == 404
        # A scratch chat (no story) is PG-13.
        scratch = http.post("/v1/experiments", json={}).json()["session_id"]
        http.post(f"/v1/experiments/{scratch}/turns", json=msg)
        assert _wait_done_or_failed(http, scratch)["error"] == nudity_reason("PG-13")


def _wait_done_or_failed(http, sid):
    deadline = time.monotonic() + 10
    while (t := http.get(f"/v1/experiments/{sid}").json()["turns"][-1])["status"] not in (
        "done",
        "failed",
    ):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return t
