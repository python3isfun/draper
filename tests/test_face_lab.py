"""Face Lab: describe a photo as a prompt, then test the prompt alone with chosen seeds."""

from drama_agent.imagegen import FixtureImageClient
from drama_agent.llm import FixtureModelClient

from .test_experiment import Chat


class Study(Chat):
    """A Face Lab study instead of a chat."""

    def __enter__(self):
        super().__enter__()
        self.sid = self.http.post("/v1/experiments", json={"kind": "face"}).json()["session_id"]
        return self

    def describe(self, **body):
        return self.http.post(f"/v1/experiments/{self.sid}/describe", json=body)

    def test(self, **body):
        return self.http.post(f"/v1/experiments/{self.sid}/tests", json=body)


def describe_calls(c):
    return [r for r in c.text.calls if r.stage == "face_describe"]


def test_a_photo_becomes_a_prompt_and_nothing_is_drawn(tmp_path):
    with Study(tmp_path) as c:
        asset = c.upload()
        r = c.describe(asset_id=asset)
        assert r.status_code == 202, r.text
        v = c.wait()
        turn = v["turns"][-1]
        assert turn["mode"] == "describe" and turn["status"] == "done"
        assert turn["prompt"].startswith("A studio casting photograph")
        assert turn["reply"] and turn["attachment_labels"] == [1]
        assert c.images.calls == []
        [req] = describe_calls(c)
        assert [m.label for m in req.media] == ["<Photo>"]
        assert "Open mode" in req.instructions
        assert v["session"]["kind"] == "face" and v["session"]["title"] == "base"
        assert v["session"]["size"] == "1728x2304"
        kinds = {s["session_id"]: s["kind"] for s in c.http.get("/v1/experiments").json()}
        assert kinds[c.sid] == "face"
        assert [s["kind"] for s in c.http.get("/v1/experiments?kind=face").json()] == ["face"]
        chats = c.http.get("/v1/experiments?kind=chat").json()
        assert c.sid not in [s["session_id"] for s in chats]


def test_describe_again_with_a_hint_builds_on_the_last_prompt(tmp_path):
    with Study(tmp_path) as c:
        c.describe(asset_id=c.upload())
        first = c.wait()["turns"][-1]
        c.describe(hint="more detail on the eyes", restrictive=True)  # same photo
        turn = c.wait()["turns"][-1]
        assert turn["attachments"] == first["attachments"]
        req = describe_calls(c)[-1]
        assert req.context == {
            "hint": "more detail on the eyes",
            "previous_prompt": first["prompt"],
        }
        assert "Restrictive mode:" in req.instructions
        assert "more detail on the eyes" in turn["prompt"]


def test_prompt_tests_use_the_given_seeds(tmp_path):
    with Study(tmp_path) as c:
        prompt = "A studio casting photograph of a woman. The image contains no text."
        r = c.test(prompt=prompt, seeds=[11, 22])
        assert r.status_code == 202, r.text
        turn = c.wait()["turns"][-1]
        assert [res["seed"] for res in turn["results"]] == [11, 22]
        assert [call[2] for call in c.images.calls] == [11, 22]
        assert all(call[3] == [] for call in c.images.calls)  # text alone, no pictures
        assert turn["size"] == "1728x2304" and turn["references"] == []
        # Without seeds: random ones, as many as asked.
        c.test(prompt=prompt, draws=3, size="4096x4096")
        turn = c.wait()["turns"][-1]
        assert len(turn["results"]) == 3 and turn["size"] == "2048x2048"  # the 2K 1:1
        # A test leaves the study as it was (no "current image").
        assert c.view()["session"]["current"] is None


def test_prompt_test_checks(tmp_path):
    with Study(tmp_path) as c:
        ok = "A photograph of a woman."
        assert c.test(prompt="  ").status_code == 422
        assert c.test(prompt=ok, seeds=[5, 5]).status_code == 422
        assert c.test(prompt=ok, seeds=[0]).status_code == 422
        assert c.test(prompt=ok, size="10x10").status_code == 422
        r = c.test(prompt="<Picture 1> is <Subject 1>.")
        assert r.status_code == 422 and "no pictures" in r.json()["detail"]
        assert c.describe().status_code == 422  # no photo yet
        r = c.send(text="hello")  # chat messages don't belong in a study
        assert r.status_code == 422


class RealFace(FixtureModelClient):
    """Says the photo shows an identifiable person, as the real describer would."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "face_describe":
            out.real_person = True
        return out


def test_renders_of_a_real_face_carry_its_likeness(tmp_path):
    with Study(tmp_path, text=RealFace()) as c:
        c.describe(asset_id=c.upload())
        assert c.wait()["turns"][-1]["likeness"] is True
        c.test(prompt="A studio casting photograph of a woman.")
        assert c.wait()["turns"][-1]["likeness"] is True


class NudePhoto(FixtureModelClient):
    async def generate(self, request, output_model):
        if request.stage == "face_describe":
            self.calls.append(request)
            return output_model(allowed=False, real_person=True, refusal="Nudity can't be drawn.")
        return await super().generate(request, output_model)


def test_a_photo_that_cant_be_described_is_refused(tmp_path):
    with Study(tmp_path, text=NudePhoto(), images=FixtureImageClient()) as c:
        c.describe(asset_id=c.upload())
        turn = c.wait()["turns"][-1]
        assert turn["refused"] and turn["error"] == "Nudity can't be drawn."
        assert turn["prompt"] == ""


def test_the_same_photo_keeps_its_number(tmp_path):
    with Study(tmp_path) as c:
        asset = c.upload()
        c.describe(asset_id=asset)
        c.wait()
        c.describe(hint="the eyes")
        c.wait()
        c.describe(asset_id=c.upload())  # a different photo gets a new number
        turns = c.wait()["turns"]
        assert [t["attachment_labels"] for t in turns] == [[1], [1], [2]]


def test_a_new_photo_starts_without_the_old_description(tmp_path):
    with Study(tmp_path) as c:
        c.describe(asset_id=c.upload())
        c.wait()
        c.describe(asset_id=c.upload())  # "Use another photo"
        c.wait()
        assert describe_calls(c)[-1].context["previous_prompt"] is None


class CitesAPicture(FixtureModelClient):
    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "face_describe":
            out.prompt = "<Picture 1> is <Subject 1>. The target image is a portrait."
        return out


def test_a_description_citing_a_picture_fails(tmp_path):
    with Study(tmp_path, text=CitesAPicture()) as c:
        c.describe(asset_id=c.upload())
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and not turn["refused"]
        assert "<Picture 1>" in turn["error"] and turn["prompt"] == ""


class FirstFaceIsReal(FixtureModelClient):
    """The first photo shows an identifiable person; later ones don't."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "face_describe":
            out.real_person = len([r for r in self.calls if r.stage == "face_describe"]) == 1
        return out


def test_tests_keep_the_likeness_of_any_described_photo(tmp_path):
    with Study(tmp_path, text=FirstFaceIsReal()) as c:
        c.describe(asset_id=c.upload())
        first = c.wait()["turns"][-1]
        c.describe(asset_id=c.upload())  # a photo with no identifiable person
        assert c.wait()["turns"][-1]["likeness"] is False
        c.test(prompt=first["prompt"])  # the first photo's description, loaded back
        assert c.wait()["turns"][-1]["likeness"] is True


def test_a_test_can_be_drawn_by_the_local_image_server(tmp_path):
    """ "Drawn with": a test names its provider; H3 stays the default."""
    local = FixtureImageClient(model_id="local (http://localhost:8080)", max_references=0)
    with Study(tmp_path, providers={"local": local}) as c:
        assert c.view()["providers"] == {"h3": c.images.model_id, "local": local.model_id}
        prompt = "A studio casting photograph of a woman. The image contains no text."
        assert c.test(prompt=prompt, seeds=[5], provider="local").status_code == 202
        turn = c.wait()["turns"][-1]
        assert turn["provider"] == "local" and turn["status"] == "done"
        assert [call[2] for call in local.calls] == [5] and c.images.calls == []
        # No photo: the likeness is judged from the words alone, and they name no one.
        assert turn["likeness"] is False
        assert c.test(prompt=prompt, seeds=[6], provider="h3").status_code == 202
        assert c.wait()["turns"][-1]["provider"] is None
        assert [call[2] for call in c.images.calls] == [6]
        r = c.test(prompt=prompt, provider="nope")
        assert r.status_code == 422 and "local" in r.json()["detail"]


def test_a_turn_naming_an_unconfigured_provider_fails_clearly():
    import pytest

    from drama_agent.experiment import ExperimentRunner
    from drama_agent.imagegen import ImageGenError

    runner = ExperimentRunner(None, None, FixtureImageClient(), None)
    assert runner._client(None)[0] is runner.images
    with pytest.raises(ImageGenError, match="'local' isn't configured"):
        runner._client("local")


def test_the_local_image_server_is_sent_a_form_and_answers_an_image():
    """curl -s localhost:8080/image -F "prompt=a red fox in fresh snow" -o fox.png"""
    import asyncio

    import httpx
    import pytest

    from drama_agent.imagegen import ImageGenError, LocalImageClient, Reference, solid_png

    png = solid_png(2, 2, b"\x10\x20\x30")
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = request.content.decode("latin-1")
        if "fail" in body:
            return httpx.Response(500, text="boom")
        if "text" in body:
            return httpx.Response(200, text="not an image", headers={"content-type": "text/plain"})
        return httpx.Response(
            200, content=png, headers={"content-type": "application/octet-stream"}
        )

    client = LocalImageClient("http://localhost:8080/", transport=httpx.MockTransport(answer))
    assert client.model_id == "local (http://localhost:8080)" and client.max_references == 0
    out = asyncio.run(client.generate("a red fox in fresh snow", "2048x2048", 7, []))
    assert out.data == png and out.content_type == "image/png"
    [req] = seen
    assert str(req.url) == "http://localhost:8080/image"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content.decode()
    for name, value in (
        ("prompt", "a red fox in fresh snow"),
        ("size", "2048x2048"),
        ("seed", "7"),
    ):
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body
    with pytest.raises(ImageGenError, match="HTTP 500"):
        asyncio.run(client.generate("fail", "2048x2048", 1, []))
    with pytest.raises(ImageGenError, match="text/plain, not a PNG or JPEG"):
        asyncio.run(client.generate("text", "2048x2048", 1, []))
    with pytest.raises(ImageGenError, match="text only"):
        asyncio.run(client.generate("x", "2048x2048", 1, [Reference("a.png", png)]))


def test_the_runner_closes_its_providers():
    import asyncio

    from drama_agent.experiment import ExperimentRunner

    class Closable(FixtureImageClient):
        closed = False

        async def aclose(self):
            self.closed = True

    local = Closable(max_references=0)
    runner = ExperimentRunner(None, None, FixtureImageClient(), None, providers={"local": local})
    asyncio.run(runner.aclose())
    assert local.closed
