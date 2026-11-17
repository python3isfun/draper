"""Shot keyframes: a checked start (and end) frame per shot, drawn from the design picks."""

import csv
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import FixtureImageClient
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.models.images import GeneratedImage
from drama_agent.models.screenplay import Intimacy
from drama_agent.shot_frames import (
    CheckItem,
    FrameCheck,
    FrameDraw,
    NotReady,
    ShotTake,
    next_round,
    plan_take,
    score,
    shot_of,
    shot_references,
)
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}


# ---------------------------------------------------------------------------
# Pure planning


def done_images(pkg, *, likeness: bool = False) -> list[GeneratedImage]:
    """A finished image for every required deliverable of the package."""
    return [
        GeneratedImage(
            image_id=f"img_{r.requirement_id}_{d.deliverable_id}"[:40],
            package_id=pkg.package_id,
            revision_id=pkg.revision_id,
            requirement_id=r.requirement_id,
            deliverable_id=d.deliverable_id,
            status="done",
            prompt="p",
            size="1024x1024",
            seed=1,
            file=f"generated/{d.deliverable_id}.png",
            likeness=likeness and r.asset_type.startswith("character"),
        )
        for r in pkg.asset_requirements.items
        for d in r.deliverables
        if d.required
    ]


def test_references_are_looks_then_set_then_props_then_faces(package):
    images = done_images(package)
    shot = max(package.shot_list, key=lambda s: len(s.character_ids))
    refs, missing = shot_references(package, images, shot)
    assert missing == []
    kinds = [r.kind for r in refs]
    n = len(shot.character_ids)
    assert kinds[:n] == ["look"] * n
    assert kinds[n] == "set"
    assert kinds[-n:] == ["face"] * n and len(refs) <= 9
    # Every picture is that shot's: its people, its place.
    assert {r.entity_id for r in refs if r.kind in ("look", "face")} == set(shot.character_ids)
    assert next(r for r in refs if r.kind == "set").entity_id == shot.location_id


def test_a_shot_waits_for_its_design_picks(package):
    shot = package.shot_list[0]
    with pytest.raises(NotReady, match="pick design images first"):
        plan_take(package, [], [], shot.shot_id)


def test_an_end_frame_is_drawn_from_the_approved_start_frame(package):
    images = done_images(package)
    shot = package.shot_list[0]
    with pytest.raises(NotReady, match="approve a start frame first"):
        plan_take(package, images, [], shot.shot_id, frame="end")
    start = plan_take(package, images, [], shot.shot_id)
    start.status, start.approved = "done", 1
    start.results = [FrameDraw(n=0, seed=5), FrameDraw(n=1, seed=6)]
    end = plan_take(package, images, [start], shot.shot_id, frame="end")
    assert end.references[0].ref_id == f"frame:{start.take_id}:1"
    assert end.references[0].kind == "start_frame"
    assert "set" not in [r.kind for r in end.references]  # the start frame shows the set


def test_score_fails_any_major_item():
    ok = CheckItem(item="count", kind="count", passed=True)
    minor = CheckItem(item="text", kind="text", passed=False, severity="minor", finding="x")
    major = CheckItem(item="Frank: face", kind="identity", passed=False, finding="older")
    assert score([ok]) == (100, True)
    assert score([ok, minor]) == (92, True)
    assert score([ok, major]) == (75, False)
    assert score([]) == (100, False)  # nothing checked is not a pass


def failed_take(package, images, round_=1) -> ShotTake:
    take = plan_take(package, images, [], package.shot_list[0].shot_id, seeds=[7, 8])
    take.round, take.status, take.prompt = round_, "done", "the old prompt"
    bad = CheckItem(item="Frank: wardrobe", kind="wardrobe", passed=False, finding="coat is blue")
    take.results = [
        FrameDraw(n=0, seed=7, check=FrameCheck(items=[bad], fix="say charcoal coat",
                                                score=75, passed=False)),
        FrameDraw(n=1, seed=8, check=FrameCheck(items=[bad, bad], score=50, passed=False)),
    ]  # fmt: skip
    return take


def test_next_round_revises_against_the_best_draw_with_the_same_seeds(package):
    take = failed_take(package, done_images(package))
    nxt = next_round(take)
    assert nxt is not None and nxt.round == 2 and nxt.run_id == take.run_id
    assert nxt.seeds == [7, 8] and nxt.results == [] and nxt.prompt == ""
    assert nxt.previous_prompt == "the old prompt"
    assert nxt.fixes == ["Frank: wardrobe: coat is blue", "Suggested fix: say charcoal coat"]
    assert next_round(failed_take(package, done_images(package), round_=3)) is None
    take.auto = False
    assert next_round(take) is None


# ---------------------------------------------------------------------------
# Through the API


class Board:
    def __init__(self, tmp_path, text=None):
        self.text = text or FixtureModelClient()
        self.images = FixtureImageClient()
        self.media = MediaStore(tmp_path)
        self.repo = InMemoryRepository()
        self.app = create_app(
            repo=self.repo, client=self.text, image_client=self.images, media=self.media,
            poll_s=0.01,
        )  # fmt: skip

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        job = wait_job(self.http, self.http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        self.pid = job["package_id"]
        self.base = f"/v1/drama-packages/{self.pid}/keyframes"
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def design(self):
        """Draw every design image and wait for them."""
        self.http.post(f"/v1/drama-packages/{self.pid}/images")
        deadline = time.monotonic() + 30
        while self.http.get(f"/v1/drama-packages/{self.pid}/images").json()["queued"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)

    def board(self) -> dict:
        return self.http.get(self.base).json()

    def wait(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while (b := self.board())["busy"]:
            assert time.monotonic() < deadline, f"{b['busy']} takes still busy"
            time.sleep(0.05)
        return b

    def shot(self, shot_id: str) -> dict:
        return next(s for s in self.board()["shots"] if s["shot_id"] == shot_id)

    def calls(self, stage: str):
        return [r for r in self.text.calls if r.stage == stage]


def test_batch_waits_for_design_picks_then_draws_and_checks_every_shot(tmp_path):
    with Board(tmp_path) as b:
        r = b.http.post(b.base, json={})
        assert r.status_code == 202 and r.json()["started"] == []
        assert all("pick design images first" in s["reason"] for s in r.json()["skipped"])
        b.design()
        r = b.http.post(b.base, json={"draws": 2})
        shots = b.board()["shots"]
        assert len(r.json()["started"]) == len(shots) and r.json()["skipped"] == []
        board = b.wait()
        for s in board["shots"]:
            [take] = s["takes"]
            assert take["status"] == "done" and take["round"] == 1
            assert take["prompt_source"] == "model" and "<Picture 1>" in take["prompt"]
            assert len(take["results"]) == 2
            assert all(d["check"]["passed"] and d["check"]["score"] == 100 for d in take["results"])
            assert s["approved"] == {"start": None, "end": None}
        # The writer saw the reference pictures; the judge saw them and the frame, last.
        [write, *_] = b.calls("shot_frames")
        assert [m.label for m in write.media][:1] == ["<Picture 1>"]
        check = b.calls("shot_check")[0]
        assert check.media[-1].label == "<Frame>"
        assert check.context["shot"]["people_count"] >= 0
        # Nothing is started twice: shots with a take running or approved are left alone.
        assert len(b.images.calls) == 2 * len(shots) + len(
            b.http.get(f"/v1/drama-packages/{b.pid}/images").json()["images"]
        )


class FailsFirstRound(FixtureModelClient):
    """The judge finds a wrong coat in the first round's draws; later rounds pass."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_check" and not request.context["prompt"].endswith("FIXED"):
            out.items.append(CheckItem(item="Frank: wardrobe", kind="wardrobe", passed=False,
                                       finding="the coat is blue"))  # fmt: skip
            out.fix = "say the coat is charcoal"
        if request.stage == "shot_frames" and request.context["fixes"]:
            out.prompt += " FIXED"
        return out


def first_shot(b) -> str:
    return b.board()["shots"][0]["shot_id"]


def test_a_failed_check_is_revised_in_a_new_round_with_the_same_seeds(tmp_path):
    with Board(tmp_path, text=FailsFirstRound()) as b:
        b.design()
        sid = first_shot(b)
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 2, "seeds": [11, 22]})
        assert r.status_code == 202, r.text
        one, two = b.wait() and b.shot(sid)["takes"]
        assert one["round"] == 1 and not any(d["check"]["passed"] for d in one["results"])
        assert one["results"][0]["check"]["score"] == 75
        assert two["round"] == 2 and two["run_id"] == one["take_id"]
        assert [d["seed"] for d in two["results"]] == [11, 22]
        assert two["fixes"] == [
            "Frank: wardrobe: the coat is blue",
            "Suggested fix: say the coat is charcoal",
        ]
        assert two["previous_prompt"] == one["prompt"]
        assert all(d["check"]["passed"] for d in two["results"])
        writer = b.calls("shot_frames")[-1]
        assert writer.context["fixes"] == two["fixes"]
        assert writer.context["previous_prompt"] == one["prompt"]


class AlwaysFails(FixtureModelClient):
    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_check":
            out.items.append(CheckItem(item="people count", kind="count", passed=False,
                                       finding="a third person"))  # fmt: skip
        return out


def test_rounds_stop_at_the_limit_and_a_person_decides(tmp_path):
    with Board(tmp_path, text=AlwaysFails()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1, "max_rounds": 3})
        b.wait()
        takes = b.shot(sid)["takes"]
        assert [t["round"] for t in takes] == [1, 2, 3]
        # A person may still approve a draw that failed; the export says it failed.
        last = takes[-1]
        r = b.http.post(f"/v1/shot-takes/{last['take_id']}/approve", json={"n": 0})
        assert r.status_code == 200 and r.json()["approved"] == 0
        z = zipfile.ZipFile(io.BytesIO(b.http.get(f"/v1/drama-packages/{b.pid}/export").content))
        [index] = [n for n in z.namelist() if n.endswith("shot_keyframes/index.csv")]
        [row] = csv.DictReader(io.StringIO(z.read(index).decode("utf-8-sig")))
        assert row["shot_id"] == sid and row["frame"] == "start" and row["check"] == "failed"
        assert "a third person" in row["failed_items"] and row["round"] == "3"
        assert any(n.endswith(f"shot_keyframes/{sid}_start.png") for n in z.namelist())


def test_approve_one_frame_then_draw_the_end_frame_from_it(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        assert b.http.post(f"{b.base}/{sid}", json={"frame": "end"}).status_code == 409
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        b.wait()
        b.http.post(f"{b.base}/{sid}", json={"draws": 1, "direction": "tighter, on Frank"})
        b.wait()
        first, second = b.shot(sid)["takes"]
        assert second["direction"] == "tighter, on Frank"
        assert b.calls("shot_frames")[-1].context["direction"] == "tighter, on Frank"
        b.http.post(f"/v1/shot-takes/{first['take_id']}/approve", json={"n": 1})
        b.http.post(f"/v1/shot-takes/{second['take_id']}/approve", json={"n": 0})
        first, second = b.shot(sid)["takes"]
        assert first["approved"] is None and second["approved"] == 0  # one per frame
        assert b.shot(sid)["approved"]["start"] == [second["take_id"], 0]
        r = b.http.post(f"{b.base}/{sid}", json={"frame": "end", "draws": 1})
        assert r.status_code == 202, r.text
        assert r.json()["references"][0]["ref_id"] == f"frame:{second['take_id']}:0"
        b.wait()
        end = b.shot(sid)["takes"][-1]
        assert end["frame"] == "end" and end["status"] == "done"
        # The start frame was sent to the image model as <Picture 1>.
        refs = b.images.calls[-1][3]
        assert refs[0].startswith(second["take_id"])
        # A take an end frame is being drawn from can't go; a finished one can.
        assert b.http.delete(f"/v1/shot-takes/{first['take_id']}").status_code == 204


def test_your_own_prompt_is_checked_and_not_rewritten(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": "<Picture 30> is <Subject 30>."})
        assert r.status_code == 422 and "<Picture 30>" in r.json()["detail"]
        mine = "The person in <Picture 1> is <Subject 1>. The target image is a still frame."
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": mine, "draws": 1})
        assert r.status_code == 202 and r.json()["auto"] is False
        take = b.wait() and b.shot(sid)["takes"][-1]
        assert take["prompt"] == mine and take["prompt_source"] == "user"
        assert b.calls("shot_frames") == []


class JudgeDown(FixtureModelClient):
    async def generate(self, request, output_model):
        if request.stage == "shot_check":
            raise RuntimeError("judge unavailable")
        return await super().generate(request, output_model)


def test_a_check_that_cant_run_is_never_a_pass(tmp_path):
    with Board(tmp_path, text=JudgeDown()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        [take] = b.shot(sid)["takes"]  # no next round: nothing to revise against
        [d] = take["results"]
        assert d["check"] is None and "judge unavailable" in d["check_error"]
        b.text.__class__ = FixtureModelClient  # the judge is back
        r = b.http.post(f"/v1/shot-takes/{take['take_id']}/check")
        assert r.status_code == 202 and r.json()["status"] == "checking"
        [take] = b.wait() and b.shot(sid)["takes"]
        assert take["status"] == "done" and take["results"][0]["check"]["passed"]
        assert len(b.calls("shot_check")) == 1  # (the failed call was never recorded)
        # Every draw has a check now: checking again redoes them all.
        b.http.post(f"/v1/shot-takes/{take['take_id']}/check")
        b.wait()
        assert len(b.calls("shot_check")) == 2


def test_an_intimate_scene_is_never_drawn_from_a_likeness(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        # The shot's scene is staged as implied intimacy, and the picks show a real likeness.
        pkg = b.repo.get_package(b.pid)
        pkg = b.repo.packages[pkg.package_id][pkg.revision_id]  # the stored one, edited in place
        shot = shot_of(pkg, sid)
        for sc in (sc for ep in pkg.episodes for sc in ep.scenes):
            if sc.scene_id == shot.scene_id:
                sc.intimacy = Intimacy(level="implied", character_ids=shot.character_ids)
        for img in b.repo.list_images(pkg.package_id, pkg.revision_id):
            img.likeness = True
            b.repo.save_image(img)
        drawn = len(b.images.calls)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        [take] = b.shot(sid)["takes"]
        assert take["status"] == "failed" and take["refused"] and take["likeness"]
        assert "content rule 3" in take["error"]
        assert len(b.images.calls) == drawn  # nothing was drawn


def test_sqlite_keeps_one_approval_per_frame(tmp_path, package):
    repo = SqliteRepository(tmp_path / "t.db")
    images = done_images(package)
    sid = package.shot_list[0].shot_id
    a = plan_take(package, images, [], sid)
    a.status, a.results = "done", [FrameDraw(n=0, seed=1)]
    b = plan_take(package, images, [a], sid)
    b.status, b.results = "done", [FrameDraw(n=0, seed=2)]
    c = plan_take(package, images, [a, b], sid)  # still running
    for t in (a, b, c):
        repo.save_shot_take(t)
    repo.approve_shot_draw(a.take_id, 0)
    repo.approve_shot_draw(b.take_id, 0)
    got = {
        t.take_id: t.approved for t in repo.list_shot_takes(package.package_id, package.revision_id)
    }
    assert got == {a.take_id: None, b.take_id: 0, c.take_id: None}
    assert repo.fail_unfinished_shot_takes("restarted") == 1
    assert repo.get_shot_take(c.take_id).status == "failed"
    repo.approve_shot_draw(b.take_id, None)
    assert repo.get_shot_take(b.take_id).approved is None


# ---------------------------------------------------------------------------
# Review fixes


def approved_pair(package):
    """(a start take with draw 0 approved, an end take drawn from it, also approved)."""
    images = done_images(package)
    sid = package.shot_list[0].shot_id
    start = plan_take(package, images, [], sid)
    start.status, start.results, start.approved = "done", [FrameDraw(n=0, seed=1)], 0
    end = plan_take(package, images, [start], sid, frame="end")
    end.status, end.results, end.approved = "done", [FrameDraw(n=0, seed=2)], 0
    return start, end


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_a_runner_saving_its_copy_never_undoes_an_approval(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    take = plan_take(package, done_images(package), [], package.shot_list[0].shot_id)
    take.results = [FrameDraw(n=0, seed=1)]
    repo.save_shot_take(take)  # the runner's copy: approved=None
    repo.approve_shot_draw(take.take_id, 0)  # the user approves meanwhile
    take.status = "done"
    repo.save_shot_take(take)  # the runner finishes, with its stale copy
    assert repo.get_shot_take(take.take_id).approved == 0
    assert repo.get_shot_take(take.take_id).status == "done"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_changing_the_start_frame_withdraws_its_end_frame(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    start, end = approved_pair(package)
    other = plan_take(package, done_images(package), [], start.shot_id)
    other.status, other.results = "done", [FrameDraw(n=0, seed=3)]
    for t in (start, end, other):
        repo.save_shot_take(t)
        repo.approve_shot_draw(t.take_id, t.approved)
    assert repo.get_shot_take(end.take_id).approved == 0
    repo.approve_shot_draw(start.take_id, 0)  # the same start again: the end frame stays
    assert repo.get_shot_take(end.take_id).approved == 0
    repo.approve_shot_draw(other.take_id, 0)  # another start: the end frame goes
    assert repo.get_shot_take(start.take_id).approved is None
    assert repo.get_shot_take(end.take_id).approved is None
    repo.approve_shot_draw(start.take_id, 0)
    repo.approve_shot_draw(end.take_id, 0)
    repo.approve_shot_draw(start.take_id, None)  # withdrawn: the end frame goes too
    assert repo.get_shot_take(end.take_id).approved is None


def test_end_frame_approval_and_deleting_the_start(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        [start] = b.wait() and b.shot(sid)["takes"]
        b.http.post(f"/v1/shot-takes/{start['take_id']}/approve", json={"n": 0})
        b.http.post(f"{b.base}/{sid}", json={"frame": "end", "draws": 1})
        end = b.wait() and b.shot(sid)["takes"][-1]
        # Another start draw is approved: the end frame was drawn from the old one.
        b.http.post(f"/v1/shot-takes/{start['take_id']}/approve", json={"n": 1})
        r = b.http.post(f"/v1/shot-takes/{end['take_id']}/approve", json={"n": 0})
        assert r.status_code == 409 and "isn't approved now" in r.json()["detail"]
        b.http.post(f"/v1/shot-takes/{start['take_id']}/approve", json={"n": 0})
        assert (
            b.http.post(f"/v1/shot-takes/{end['take_id']}/approve", json={"n": 0}).status_code
            == 200
        )
        # Deleting the approved start take withdraws the end frame drawn from it.
        assert b.http.delete(f"/v1/shot-takes/{start['take_id']}").status_code == 204
        assert b.shot(sid)["approved"] == {"start": None, "end": None}


def test_batch_with_no_shots_starts_nothing(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        r = b.http.post(b.base, json={"shot_ids": []})
        assert r.json() == {"started": [], "skipped": []} and b.board()["busy"] == 0


def test_a_refused_take_is_not_retried(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        bad = "The person in <Picture 1> is <Subject 1>, nude on the bed."
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": bad, "draws": 1})
        if r.status_code == 202:  # the screen let it through; the runner's guard refuses it
            [take] = b.wait() and b.shot(sid)["takes"]
            assert take["refused"]
            r = b.http.post(f"/v1/shot-takes/{take['take_id']}/retry")
            assert r.status_code == 409 and "content rules" in r.json()["detail"]
        else:
            assert r.status_code == 422


def test_export_follows_the_shot_list_order(package):
    from drama_agent.export import approved_keyframes

    images = done_images(package)
    takes = []
    for shot in reversed(package.shot_list[:3]):
        t = plan_take(package, images, takes, shot.shot_id)
        t.status, t.results, t.approved = "done", [FrameDraw(n=0, seed=1)], 0
        takes.append(t)
    twice = takes[0].model_copy(update={"take_id": "stk_again"})  # a stray double approval
    got = approved_keyframes(package, [*takes, twice])
    assert [t.shot_id for t, _ in got] == [s.shot_id for s in package.shot_list[:3]]


def test_a_png_start_frame_is_sent_to_the_model_as_its_jpeg_thumbnail(package, tmp_path):
    from drama_agent.shot_frames import ShotFrameRunner

    repo = InMemoryRepository()
    media = MediaStore(tmp_path)
    start, _ = approved_pair(package)
    start.results[0] = FrameDraw(n=0, seed=1, file="k/a.png", thumb="k/a_thumb.jpg",
                                 content_type="image/png")  # fmt: skip
    repo.save_shot_take(start)
    runner = ShotFrameRunner(repo, media, FixtureImageClient(), FixtureModelClient())
    path, mime = runner._ref_file(f"frame:{start.take_id}:0", "thumb")
    assert path.name == "a_thumb.jpg" and mime == "image/jpeg"
    assert runner._ref_file(f"frame:{start.take_id}:0")[1] == "image/png"
