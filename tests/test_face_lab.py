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
        assert v["session"]["size"] == "3552x4736"
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
        assert turn["size"] == "3552x4736" and turn["references"] == []
        # Without seeds: random ones, as many as asked.
        c.test(prompt=prompt, draws=3, size="4096x4096")
        turn = c.wait()["turns"][-1]
        assert len(turn["results"]) == 3 and turn["size"] == "4096x4096"
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
