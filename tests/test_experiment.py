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
            return output_model(real_person=False, prompt="x" * 7000, reply="long")
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
        assert turn["size"] == "1728x2304"  # "auto": the 3:4 upload's aspect ratio, at 2K
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
        # An earlier 16 MP size (a client's saved choice) draws at the same aspect's 2K size.
        c.send(text="a red bicycle against a white wall", size="4736x3552")
        turn = c.wait()["turns"][-1]
        assert turn["references"] == [] and turn["size"] == "2304x1728"
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


class DrawsWriter(FixtureModelClient):
    """A writer that reads a draw count from the message (as "do 4 draws" would)."""

    def __init__(self, draws):
        super().__init__()
        self.draws = draws

    async def generate(self, request, output_model):
        if request.stage == "experiment":
            self.calls.append(request)
            return output_model(
                real_person=False,
                draws=self.draws,
                prompt="A cat on a sofa. The image contains no text, letters or numbers.",
                reply="Four draws of one cat.",
            )
        return await super().generate(request, output_model)


def writer_call(c):
    return next(r for r in reversed(c.text.calls) if r.stage == "experiment")


@pytest.mark.parametrize(("asked", "rendered"), [(4, 4), (9, 4), (0, 1)])
def test_writer_sets_the_number_of_draws(tmp_path, asked, rendered):
    with Chat(tmp_path, text=DrawsWriter(asked)) as c:
        c.send(text=f"a cat, do {asked} draws")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "done", turn["error"]
        assert turn["draws"] == rendered and len(turn["results"]) == rendered
        assert len(c.images.calls) == rendered
        assert len({r["seed"] for r in turn["results"]}) == rendered  # one seed per draw
        assert writer_call(c).context["draws"] == 1  # the writer saw the user's setting


def test_writer_without_a_draw_count_keeps_the_setting(tmp_path):
    with Chat(tmp_path, text=DrawsWriter(None)) as c:
        c.send(text="a cat", draws=2)
        turn = c.wait()["turns"][-1]
        assert turn["draws"] == 2 and len(turn["results"]) == 2
        assert writer_call(c).context["draws"] == 2


def test_writer_is_told_to_use_draws_not_grids():
    from drama_agent.experiment import writer_instructions

    text = writer_instructions()
    assert "`draws`" in text and "Never show alternatives as a grid" in text
    assert "1 to 4" in text


def test_fallback_text_gets_the_exact_prompt_check(tmp_path):
    with Chat(tmp_path, text=BrokenWriter()) as c:
        c.send(text="<Picture 2> is a hat")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and "<Picture 2>" in turn["error"]
        assert c.images.calls == []


# ---------------------------------------------------------------------------
# Image numbers (#N) and mentions


class Picker(FixtureModelClient):
    """A writer that chooses which pictures to send (as "go back to #N" would)."""

    def __init__(self, pictures):
        super().__init__()
        self.pictures = pictures

    async def generate(self, request, output_model):
        if request.stage == "experiment":
            self.calls.append(request)
            n = len(request.context["pictures"] if self.pictures is None else self.pictures)
            defs = " ".join(f"<Picture {i}> is <Subject {i}>." for i in range(1, n + 1))
            return output_model(
                real_person=False,
                pictures=self.pictures,
                prompt=f"{defs} The target image is <Subject 1> with a hat. No other lettering is added.",
                reply="Back to the earlier one, with a hat.",
            )
        return await super().generate(request, output_model)


def labels(turn):
    return turn["attachment_labels"] + [r["label"] for r in turn["results"]]


def test_every_image_gets_a_number(tmp_path):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])
        c.send(text="add a hat")
        c.wait()
        c.send(text="add a scarf", draws=2)
        turns = c.wait()["turns"]
        assert [labels(t) for t in turns] == [[1], [2], [3, 4]]
        assert c.view()["session"]["current"] == f"turn:{turns[2]['turn_id']}:0"


def test_mentioned_images_are_offered_to_the_writer(tmp_path):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])  # #1
        c.send(text="add a hat")  # #2
        c.wait()
        c.send(text="add a scarf")  # #3, current
        c.wait()
        c.send(text="give her the hair from #1")
        turn = c.wait()["turns"][-1]
        assert turn["references"][0].startswith("turn:")  # the current image (#3) first
        assert turn["references"][1] == c.view()["turns"][0]["references"][0]  # then #1
        ctx = c.text.calls[-1].context
        assert [p["id"] for p in ctx["pictures"]] == ["#3", "#1"]
        assert ctx["pictures"][0]["made_by_prompt"]  # the prompt that made #3
        assert c.send(text="like #99").json()["detail"] == "There's no image #99 in this chat."


def test_writer_can_go_back_to_an_earlier_image(tmp_path):
    with Chat(tmp_path, text=Picker(["#2"])) as c:
        c.send(text="a red bicycle")  # #1
        c.wait()
        c.send(text="make it blue")  # #2
        c.wait()
        c.send(text="make it green")  # #3: worse
        c.wait()
        first = c.view()["turns"]
        c.send(text="go back to #2 and add a basket")
        turn = c.wait()["turns"][-1]
        assert turn["references"] == [f"turn:{first[1]['turn_id']}:0"]  # only #2, as Picture 1
        assert c.images.calls[-1][3] == [f"{first[1]['turn_id']}_0.png"]
        assert labels(turn) == [4]


def test_unknown_picture_choice_falls_back_to_the_message(tmp_path):
    with Chat(tmp_path, text=Picker(["#42"])) as c:
        c.send(text="a red bicycle")
        c.wait()
        c.send(text="like #1 but at night")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "done"
        assert turn["prompt"] == "like <Picture 1> but at night"  # #1 is the current image
        assert "Couldn't rewrite" in turn["reply"]


def test_exact_prompts_turn_numbers_into_pictures(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a red bicycle")  # #1
        c.wait()
        c.send(text="a blue bicycle")  # #2, current
        c.wait()
        prompt = "<Picture 1> is <Subject 1>. #1 is <Subject 2>. The target image ..."
        c.send(text=prompt, mode="exact")
        turn = c.wait()["turns"][-1]
        assert turn["prompt"] == (
            "<Picture 1> is <Subject 1>. <Picture 2> is <Subject 2>. The target image ..."
        )
        assert len(turn["references"]) == 2


def test_images_from_before_numbering_get_numbers(tmp_path):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])
        c.send(text="add a hat")
        c.wait()
        repo = c.app.state.repo
        for t in repo.list_exp_turns(c.sid):  # as saved before numbering existed
            t.attachment_labels = []
            for r in t.results:
                r.label = None
            repo.save_exp_turn(t)
        turns = c.view()["turns"]
        assert [labels(t) for t in turns] == [[3], [4]]  # oldest first, numbers never reused
        assert [labels(t) for t in c.view()["turns"]] == [[3], [4]]  # once


def test_numbers_in_quotes_and_colors_are_not_mentions(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a red door")  # #1
        c.wait()
        assert c.send(text="paint the wall #222222").status_code == 202  # a color
        c.wait()
        prompt = '<Picture 1> is <Subject 1>. Like #1, and a plaque reads "Room #1".'
        c.send(text=prompt, mode="exact")
        turn = c.wait()["turns"][-1]
        assert turn["prompt"] == (
            '<Picture 1> is <Subject 1>. Like <Picture 2>, and a plaque reads "Room #1".'
        )
        assert c.send(text="like #99").status_code == 422  # still a likely typo


def test_sending_numbers_old_images_first(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a red door")
        c.wait()
        repo = c.app.state.repo
        for t in repo.list_exp_turns(c.sid):  # as saved before numbering existed
            for r in t.results:
                r.label = None
            repo.save_exp_turn(t)
        # No GET in between (an API client): sending numbers the old image first.
        r = c.send(text="like #2 but blue")
        assert r.status_code == 202, r.text


def test_auto_size_from_an_old_16mp_turn_draws_at_2k() -> None:
    """Editing an image a chat made at 16 MP draws the follow-up at the same aspect's 2K size."""
    from drama_agent.experiment import SIZES, _size_for

    s = new_session("t")
    old = ExpTurn(
        turn_id="turn_old", session_id=s.session_id, index=1, text="a", status="done",
        size="4096x4096",
    )  # fmt: skip
    assert _size_for("auto", ["turn:turn_old:0"], [old], {}) == SIZES["1:1"]
    old.size = "3552x4736"
    assert _size_for("auto", ["turn:turn_old:0"], [old], {}) == SIZES["3:4"]
