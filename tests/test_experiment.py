"""Experiment tab: chat sessions that change an image with the image model, turn by turn."""

import time

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.experiment import ExpTurn, new_session
from drama_agent.imagegen import FixtureImageClient, ImageGenError, solid_png
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

PORTRAIT = solid_png(30, 40, b"\x10\x20\x30")  # 3:4


class FlakyImages(FixtureImageClient):
    """Fails the first `fail` calls."""

    def __init__(self, fail: int = 1, **kw):
        super().__init__(**kw)
        self.fail = fail

    async def generate(self, prompt, size, seed, references):
        if self.fail:
            self.fail -= 1
            raise ImageGenError("image generation failed: HTTP 500")
        return await super().generate(prompt, size, seed, references)


class LongWriter(FixtureModelClient):
    async def generate(self, request, output_model):
        if request.stage == "experiment":
            return output_model(prompt="x" * 7000, reply="long")
        return await super().generate(request, output_model)


class BrokenWriter(FixtureModelClient):
    async def generate(self, request, output_model):
        if request.stage == "experiment":
            raise RuntimeError("model down")
        return await super().generate(request, output_model)


class Chat:
    def __init__(self, tmp_path, images=None, text=None):
        self.text = text or FixtureModelClient()
        self.images = images or FixtureImageClient()
        self.app = create_app(
            client=self.text, image_client=self.images, media=MediaStore(tmp_path), poll_s=0.01
        )

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        self.sid = self.http.post("/v1/experiments", json={}).json()["session_id"]
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def upload(self) -> str:
        r = self.http.post("/v1/source-assets", params={"filename": "base.png"}, content=PORTRAIT)
        assert r.status_code == 201, r.text
        return r.json()["asset_id"]

    def send(self, **body):
        return self.http.post(f"/v1/experiments/{self.sid}/turns", json=body)

    def view(self) -> dict:
        return self.http.get(f"/v1/experiments/{self.sid}").json()

    def wait(self, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            v = self.view()
            if not any(t["status"] in ("pending", "running") for t in v["turns"]):
                return v
            assert time.monotonic() < deadline, "turn still running"
            time.sleep(0.02)


def test_upload_then_chat_edits_the_current_image(tmp_path):
    with Chat(tmp_path) as c:
        asset = c.upload()
        up = c.send(attachments=[asset])
        assert up.status_code == 202
        assert up.json()["mode"] == "upload" and up.json()["status"] == "done"
        assert c.view()["session"]["current"] == f"asset:{asset}"
        assert c.images.calls == []  # an upload alone renders nothing

        assert c.send(text="put her hair in a low bun").status_code == 202
        v = c.wait()
        turn = v["turns"][-1]
        assert turn["status"] == "done", turn["error"]
        assert turn["references"] == [f"asset:{asset}"]
        assert turn["size"] == "3552x4736"  # "auto": the 3:4 upload's aspect ratio
        assert "<Picture 1> is <Subject 1>" in turn["prompt"]  # rewritten, not as typed
        assert turn["reply"] and turn["prompt_model"] == "fixture-v1"
        prompt, _, _, refs = c.images.calls[-1]
        assert prompt == turn["prompt"] and len(refs) == 1
        # The writer saw the picture, labeled.
        req = c.text.calls[-1]
        assert [m.label for m in req.media] == ["<Picture 1>"]
        assert req.context["message"] == "put her hair in a low bun"
        # The result is now the current image, and the next message edits it.
        assert v["session"]["current"] == f"turn:{turn['turn_id']}:0"
        assert v["session"]["title"] == "put her hair in a low bun"
        img = c.http.get(f"/v1/experiment-turns/{turn['turn_id']}/results/0/content")
        assert img.status_code == 200 and img.content.startswith(b"\x89PNG")

        c.send(text="now make her smile")
        v = c.wait()
        assert v["turns"][-1]["references"] == [f"turn:{turn['turn_id']}:0"]
        assert c.text.calls[-1].context["history"][-1]["message"] == "put her hair in a low bun"
        assert c.text.calls[-1].context["current_image_prompt"] == turn["prompt"]


def test_exact_prompt_is_sent_as_written_and_checked(tmp_path):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])
        bad = c.send(text="<Picture 2> is <Subject 2>.", mode="exact")
        assert bad.status_code == 422 and "<Picture 2>" in bad.json()["detail"]
        prompt = "<Picture 1> is <Subject 1>. The target image is <Subject 1> in blue light."
        assert c.send(text=prompt, mode="exact", draws=2).status_code == 202
        turn = c.wait()["turns"][-1]
        assert turn["prompt"] == prompt and len(turn["results"]) == 2
        assert [call[0] for call in c.images.calls] == [prompt, prompt]
        assert turn["results"][0]["seed"] != turn["results"][1]["seed"]


def test_new_image_without_a_current_one(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a red bicycle against a white wall", size="4736x3552")
        turn = c.wait()["turns"][-1]
        assert turn["references"] == [] and turn["size"] == "4736x3552"
        assert c.images.calls[-1][3] == []
        assert c.text.calls[-1].context["pictures"] == []


def test_choose_current_and_clear(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a cat", draws=2)
        turn = c.wait()["turns"][-1]
        url = f"/v1/experiments/{c.sid}"
        ref = f"turn:{turn['turn_id']}:1"
        assert c.http.patch(url, json={"current": ref}).json()["current"] == ref
        assert c.http.patch(url, json={"current": "turn:turn_nope:0"}).status_code == 422
        assert c.http.patch(url, json={"clear_current": True}).json()["current"] is None
        assert c.http.patch(url, json={"size": "123x45"}).status_code == 422


def test_one_message_at_a_time(tmp_path):
    with Chat(tmp_path, images=FixtureImageClient(delay_s=0.5)) as c:
        assert c.send(text="a cat").status_code == 202
        busy = c.send(text="a dog")
        assert busy.status_code == 409
        assert c.http.delete(f"/v1/experiments/{c.sid}").status_code == 409
        c.wait()
        assert c.send(text="a dog").status_code == 202
        c.wait()


def test_failed_turn_keeps_its_prompt_and_retries(tmp_path):
    with Chat(tmp_path, images=FlakyImages(fail=1)) as c:
        c.send(text="a cat")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and "HTTP 500" in turn["error"]
        assert turn["prompt"]  # written before the render failed
        writes = len(c.text.calls)
        r = c.http.post(f"/v1/experiment-turns/{turn['turn_id']}/retry")
        assert r.status_code == 202
        done = c.wait()["turns"][-1]
        assert done["status"] == "done" and done["prompt"] == turn["prompt"]
        assert len(c.text.calls) == writes  # the prompt isn't written again
        assert c.http.post(f"/v1/experiment-turns/{turn['turn_id']}/retry").status_code == 409


def test_writer_failure_sends_the_message_as_written(tmp_path):
    with Chat(tmp_path, text=BrokenWriter()) as c:
        c.send(text="a cat on a red sofa")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "done"
        assert turn["prompt"] == "a cat on a red sofa"
        assert "Couldn't rewrite" in turn["reply"]


def test_delete_session(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a cat")
        turn = c.wait()["turns"][-1]
        assert (tmp_path / turn["results"][0]["file"]).is_file()
        assert c.http.delete(f"/v1/experiments/{c.sid}").status_code == 204
        assert c.http.get(f"/v1/experiments/{c.sid}").status_code == 404
        assert not (tmp_path / "experiments" / c.sid).exists()
        assert c.http.get("/v1/experiments").json() == []


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_repository_roundtrip(tmp_path, kind):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "x.db")
    s = new_session("Lila")
    repo.save_exp_session(s)
    a = ExpTurn(turn_id="turn_a", session_id=s.session_id, index=1, text="a", status="done")
    b = ExpTurn(turn_id="turn_b", session_id=s.session_id, index=2, text="b", status="running")
    repo.save_exp_turn(b)
    repo.save_exp_turn(a)
    assert [t.turn_id for t in repo.list_exp_turns(s.session_id)] == ["turn_a", "turn_b"]
    assert repo.fail_unfinished_exp_turns("restarted") == 1
    assert repo.get_exp_turn("turn_b").status == "failed"
    assert repo.get_exp_turn("turn_b").error == "restarted"
    assert [x.title for x in repo.list_exp_sessions()] == ["Lila"]
    repo.delete_exp_session(s.session_id)
    assert repo.get_exp_session(s.session_id) is None
    assert repo.list_exp_turns(s.session_id) == []


def test_uploads_go_to_the_image_model_as_uploaded(tmp_path, monkeypatch):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])
        c.send(text="add a hat")
        c.wait()
        assert c.images.calls[-1][3] == ["original.png"]  # not the 1536 px copy
        # Over H3's reference budget, uploads fall back to the downscaled copy.
        monkeypatch.setattr("drama_agent.experiment.MAX_REFERENCE_BYTES", 10)
        c.http.patch(f"/v1/experiments/{c.sid}", json={"current": c.view()["session"]["current"]})
        c.send(text="add a scarf", attachments=[c.upload()])
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "done", turn["error"]
        assert c.images.calls[-1][3][1] == "model.jpg"


def test_current_image_is_locked_while_a_message_runs(tmp_path):
    with Chat(tmp_path, images=FixtureImageClient(delay_s=0.5)) as c:
        c.send(text="a cat")
        url = f"/v1/experiments/{c.sid}"
        assert c.http.patch(url, json={"clear_current": True}).status_code == 409
        assert c.http.patch(url, json={"title": "Cats"}).status_code == 200  # still allowed
        turn = c.wait()["turns"][-1]
        assert c.view()["session"]["current"] == f"turn:{turn['turn_id']}:0"


def test_delete_removes_uploads_nothing_else_uses(tmp_path):
    with Chat(tmp_path) as c:
        own, shared = c.upload(), c.upload()
        c.send(attachments=[own, shared])
        other = c.http.post("/v1/experiments", json={}).json()["session_id"]
        c.http.post(f"/v1/experiments/{other}/turns", json={"attachments": [shared]})
        assert c.http.delete(f"/v1/experiments/{c.sid}").status_code == 204
        assert c.http.get(f"/v1/source-assets/{own}").status_code == 404
        assert not (tmp_path / own).exists()
        assert c.http.get(f"/v1/source-assets/{shared}").status_code == 200


def test_writer_output_is_checked(tmp_path):
    with Chat(tmp_path, text=LongWriter()) as c:
        c.send(text="a cat")
        turn = c.wait()["turns"][-1]
        assert turn["prompt"] == "a cat"  # too long to send: falls back, never cut
        assert "longer than" in turn["reply"]


def test_fallback_text_gets_the_exact_prompt_check(tmp_path):
    with Chat(tmp_path, text=BrokenWriter()) as c:
        c.send(text="<Picture 2> is a hat")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and "<Picture 2>" in turn["error"]
        assert c.images.calls == []
