"""Shot keyframes: a sub-shot plan per shot and a checked keyframe per sub-shot."""

import csv
import io
import json
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
    FrameRef,
    NotReady,
    PlannedSubShot,
    PlanOutput,
    ShotFrameRunner,
    ShotPicks,
    ShotPlan,
    ShotTake,
    current_sub,
    make_sub_shots,
    next_round,
    plan_of,
    plan_take,
    planned_sub_shots,
    score,
    shot_of,
    shot_references,
    single_plan,
    stale_approvals,
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


def busiest(pkg):
    """The shot with the most people (at least two in the sample)."""
    return max(pkg.shot_list, key=lambda s: (len(s.character_ids), s.duration_ms))


def two_cuts(pkg, shot) -> ShotPlan:
    """The shot as a wide on everyone, then a close-up on its first person."""
    half = shot.duration_ms // 2
    subs = make_sub_shots(shot, [
        {"start_ms": 0, "framing": "wide", "action": "they talk", "people": shot.character_ids},
        {"start_ms": half, "framing": "close-up", "camera": "still", "action": "a reaction",
         "people": shot.character_ids[:1]},
    ])  # fmt: skip
    return ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id,
                    shot_id=shot.shot_id, source="user", sub_shots=subs)  # fmt: skip


def finished(take: ShotTake, approved: int | None = 0) -> ShotTake:
    take.status, take.results, take.approved = "done", [FrameDraw(n=0, seed=1)], approved
    return take


def test_references_are_looks_then_set_then_props_then_faces(package):
    images = done_images(package)
    shot = busiest(package)
    refs, missing = shot_references(package, images, shot)
    assert missing == []
    kinds = [r.kind for r in refs]
    n = len(shot.character_ids)
    assert kinds[:n] == ["look"] * n
    assert kinds[n] == "set"
    assert kinds[-n:] == ["face"] * n and len(refs) <= 9
    assert {r.entity_id for r in refs if r.kind in ("look", "face")} == set(shot.character_ids)
    assert next(r for r in refs if r.kind == "set").entity_id == shot.location_id


def test_a_shot_waits_for_its_design_picks(package):
    shot = package.shot_list[0]
    with pytest.raises(NotReady, match="first: "):
        plan_take(package, [], [], single_plan(package, shot))


def test_a_shot_without_a_plan_is_one_sub_shot(package):
    shot = package.shot_list[0]
    [sub] = single_plan(package, shot).sub_shots
    assert (sub.index, sub.start_ms, sub.end_ms) == (1, 0, shot.duration_ms)
    assert sub.framing == shot.framing and sub.people == shot.character_ids
    assert sub.timecode == "00:00.000"


def test_sub_shots_are_checked(package):
    shot = busiest(package)
    row = {"framing": "wide", "action": "x", "people": []}
    two = make_sub_shots(shot, [{**row, "start_ms": 0}, {**row, "start_ms": 2500}])
    assert two[1].timecode == "00:02.500" and two[0].end_ms == 2500
    assert two[1].end_ms == shot.duration_ms
    bad = [
        ([{**row, "start_ms": 500}], "starts at 0"),
        ([{**row, "start_ms": 0}, {**row, "start_ms": 300}], "at least 1 s"),
        ([{**row, "start_ms": 0}, {**row, "start_ms": shot.duration_ms}], "at least 1 s"),
        ([{**row, "start_ms": 0}] * 4, "1 to 3"),
        ([{**row, "start_ms": 0, "people": ["ch_nobody"]}], "isn't in the shot"),
        ([{**row, "start_ms": 0, "framing": " "}], "needs a framing"),
    ]
    for rows, message in bad:
        with pytest.raises(ValueError, match=message):
            make_sub_shots(shot, rows)


def test_the_planner_names_become_character_ids(package):
    shot = busiest(package)
    names = {c.character_id: c.name for c in package.story_bible.characters}
    first = names[shot.character_ids[0]]
    out = PlanOutput(sub_shots=[
        PlannedSubShot(start_s=0, framing="wide", action="a", people=[first.upper()]),
        PlannedSubShot(start_s=1.5, framing="close", action="b", people=[first]),
    ])  # fmt: skip
    subs = planned_sub_shots(package, shot, out)
    assert [s.people for s in subs] == [[shot.character_ids[0]]] * 2
    assert subs[1].start_ms == 1500
    out.sub_shots[1].people = ["Nobody Here"]
    with pytest.raises(ValueError, match="isn't one of the shot's people"):
        planned_sub_shots(package, shot, out)
    # Small slips are forgiven: a name with a surname or title, a first cut just after 0.
    first_name = first.split()[0]
    out.sub_shots[0].start_s = 0.2
    out.sub_shots[1].people = [f"Ms. {first_name} Somebody"]
    subs = planned_sub_shots(package, shot, out)
    assert subs[0].start_ms == 0 and subs[1].people == [shot.character_ids[0]]


def test_a_later_sub_shot_is_drawn_from_the_approved_keyframe_before_it(package):
    images = done_images(package)
    shot = busiest(package)
    plan = two_cuts(package, shot)
    with pytest.raises(NotReady, match="approved keyframe for sub-shot 1"):
        plan_take(package, images, [], plan, sub_shot=2)
    one = finished(plan_take(package, images, [], plan))
    two = plan_take(package, images, [one], plan, sub_shot=2)
    assert two.references[0].ref_id == f"frame:{one.take_id}:0"
    assert two.references[0].kind == "previous_frame"
    # Only the close-up's person is sent (look and face); the set and props too.
    people = {r.entity_id for r in two.references if r.kind in ("look", "face")}
    assert people == {shot.character_ids[0]}
    assert two.spec == plan.sub(2) and two.sub_shot == 2


def test_score_fails_any_major_item():
    ok = CheckItem(item="count", kind="count", passed=True)
    minor = CheckItem(item="text", kind="text", passed=False, severity="minor", finding="x")
    major = CheckItem(item="Frank: face", kind="identity", passed=False, finding="older")
    assert score([ok]) == (100, True)
    assert score([ok, minor]) == (92, True)
    assert score([ok, major]) == (75, False)
    assert score([]) == (100, False)  # nothing checked is not a pass


def failed_take(package, images, round_=1) -> ShotTake:
    plan = single_plan(package, package.shot_list[0])
    take = plan_take(package, images, [], plan, seeds=[7, 8])
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
    assert nxt.previous_prompt == "the old prompt" and nxt.spec == take.spec
    assert nxt.fixes == ["Frank: wardrobe: coat is blue", "Suggested fix: say charcoal coat"]
    assert next_round(failed_take(package, done_images(package), round_=3)) is None
    take.auto = False
    assert next_round(take) is None


def test_takes_from_before_sub_shots_still_load():
    base = {"take_id": "t", "package_id": "p", "revision_id": "r", "shot_id": "s",
            "run_id": "t", "size": "1x1"}  # fmt: skip
    assert ShotTake.model_validate({**base, "frame": "start"}).sub_shot == 1
    assert ShotTake.model_validate({**base, "frame": "end"}).sub_shot == 0


# ---------------------------------------------------------------------------
# Approvals


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_a_runner_saving_its_copy_never_undoes_an_approval(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    plan = single_plan(package, package.shot_list[0])
    take = plan_take(package, done_images(package), [], plan)
    take.results = [FrameDraw(n=0, seed=1)]
    repo.save_shot_take(take)  # the runner's copy: approved=None
    repo.approve_shot_draw(take.take_id, 0)  # the user approves meanwhile
    take.status = "done"
    repo.save_shot_take(take)  # the runner finishes, with its stale copy
    assert repo.get_shot_take(take.take_id).approved == 0
    assert repo.get_shot_take(take.take_id).status == "done"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_changing_an_earlier_keyframe_withdraws_the_later_ones(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    images = done_images(package)
    plan = two_cuts(package, busiest(package))
    one = finished(plan_take(package, images, [], plan))
    two = finished(plan_take(package, images, [one], plan, sub_shot=2))
    other = finished(plan_take(package, images, [one, two], plan), approved=None)
    for t in (one, two, other):
        repo.save_shot_take(t)
        if t.approved is not None:
            repo.approve_shot_draw(t.take_id, 0)
    assert repo.get_shot_take(two.take_id).approved == 0
    repo.approve_shot_draw(one.take_id, 0)  # the same keyframe again: the next stays
    assert repo.get_shot_take(two.take_id).approved == 0
    repo.approve_shot_draw(other.take_id, 0)  # another sub-shot 1 keyframe: the next goes
    assert repo.get_shot_take(one.take_id).approved is None
    assert repo.get_shot_take(two.take_id).approved is None
    repo.approve_shot_draw(one.take_id, 0)
    repo.approve_shot_draw(two.take_id, 0)
    repo.approve_shot_draw(one.take_id, None)  # withdrawn: the next goes too
    assert repo.get_shot_take(two.take_id).approved is None


def test_a_changed_plan_makes_its_keyframes_stale(package):
    images = done_images(package)
    shot = busiest(package)
    plan = two_cuts(package, shot)
    one = finished(plan_take(package, images, [], plan))
    two = finished(plan_take(package, images, [one], plan, sub_shot=2))
    assert stale_approvals(package, shot, [one, two], plan) == []
    changed = plan.model_copy(deep=True)
    changed.sub_shots[1].action = "a different reaction"
    assert stale_approvals(package, shot, [one, two], changed) == [two]
    assert stale_approvals(package, shot, [one, two], single_plan(package, shot)) == [one, two]


def test_a_take_from_before_sub_shots_matches_only_the_whole_shot(package):
    shot = busiest(package)
    old = finished(plan_take(package, done_images(package), [], single_plan(package, shot)))
    old.spec = None  # as loaded from a v8 database
    assert current_sub(package, shot, single_plan(package, shot), old) is not None
    plan = two_cuts(package, shot)
    assert current_sub(package, shot, plan, old) is None
    assert stale_approvals(package, shot, [old], plan) == [old]


def test_nothing_is_drawn_while_a_first_plan_is_written(package):
    shot = package.shot_list[0]
    writing = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                       shot_id=shot.shot_id, status="writing")  # fmt: skip
    plan = plan_of(package, shot, writing)
    assert plan.busy and len(plan.sub_shots) == 1
    with pytest.raises(NotReady, match="still being planned"):
        plan_take(package, done_images(package), [], plan)


def test_a_sub_shot_needs_only_its_own_peoples_picks(package):
    shot = busiest(package)
    other = shot.character_ids[1]
    images = [i for i in done_images(package)
              if not (i.requirement_id.startswith("ar_character") and other in i.requirement_id)]  # fmt: skip
    plan = two_cuts(package, shot)  # sub-shot 2: only the first person
    with pytest.raises(NotReady, match="first: "):
        plan_take(package, images, [], plan)  # sub-shot 1 has both people
    one = finished(plan_take(package, done_images(package), [], plan))
    two = plan_take(package, images, [one], plan, sub_shot=2)  # not blocked by the other
    assert {r.entity_id for r in two.references if r.kind in ("look", "face")} == {
        shot.character_ids[0]
    }


def test_the_nine_picture_cap_is_applied_per_sub_shot():
    def ref(kind, who):
        return FrameRef(ref_id=f"img_{kind}_{who}", role=who, kind=kind, entity_id=who)

    crowd = [f"ch_{i}" for i in range(7)]
    picks = ShotPicks(
        looks=[ref("look", c) for c in crowd], faces=[ref("face", c) for c in crowd],
        sets=[ref("set", "loc")], props=[ref("prop", "p1"), ref("prop", "p2")], missing=[],
    )  # fmt: skip
    everyone = picks.refs()
    assert len(everyone) == 9 and not any(r.kind == "face" for r in everyone)
    prev = FrameRef(ref_id="frame:t:0", role="before", kind="previous_frame")
    close = picks.refs({"ch_6"}, prev)
    assert [r.kind for r in close] == ["previous_frame", "look", "set", "prop", "prop", "face"]
    assert close[-1].entity_id == "ch_6"


def test_export_follows_the_shot_list_and_the_plan(package):
    from drama_agent.export import approved_keyframes

    images = done_images(package)
    takes = []
    for shot in reversed(package.shot_list[:3]):
        takes.append(finished(plan_take(package, images, takes, single_plan(package, shot))))
    twice = takes[0].model_copy(update={"take_id": "stk_again"})  # a stray double approval
    got = approved_keyframes(package, [*takes, twice], [])
    assert [t.shot_id for t, _, _ in got] == [s.shot_id for s in package.shot_list[:3]]
    # A keyframe drawn for a sub-shot the plan has since changed is left out.
    first = package.shot_list[0]
    if len(first.character_ids) and first.duration_ms >= 2000:
        plan = two_cuts(package, first)
        got = approved_keyframes(package, takes, [plan])
        assert first.shot_id not in [t.shot_id for t, _, _ in got]


def test_a_png_keyframe_is_sent_to_the_model_as_its_jpeg_thumbnail(package, tmp_path):
    repo = InMemoryRepository()
    plan = single_plan(package, package.shot_list[0])
    take = finished(plan_take(package, done_images(package), [], plan))
    take.results[0] = FrameDraw(n=0, seed=1, file="k/a.png", thumb="k/a_thumb.jpg",
                                content_type="image/png")  # fmt: skip
    repo.save_shot_take(take)
    runner = ShotFrameRunner(repo, MediaStore(tmp_path), FixtureImageClient(), FixtureModelClient())
    path, mime = runner._ref_file(f"frame:{take.take_id}:0", "thumb")
    assert path.name == "a_thumb.jpg" and mime == "image/jpeg"
    assert runner._ref_file(f"frame:{take.take_id}:0")[1] == "image/png"


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
            assert time.monotonic() < deadline, f"{b['busy']} still busy"
            time.sleep(0.05)
        return b

    def shot(self, shot_id: str) -> dict:
        return next(s for s in self.board()["shots"] if s["shot_id"] == shot_id)

    def takes(self, shot_id: str, sub: int = 1) -> list[dict]:
        return self.shot(shot_id)["sub_shots"][sub - 1]["takes"]

    def calls(self, stage: str):
        return [r for r in self.text.calls if r.stage == stage]

    def busiest(self) -> dict:
        return max(self.board()["shots"], key=lambda s: (len(s["people"]), s["duration_ms"]))

    def two_cuts(self, shot: dict) -> dict:
        half = shot["duration_ms"] // 2
        ids = [p["id"] for p in shot["people"]]
        return {"sub_shots": [
            {"start_ms": 0, "framing": "wide", "action": "they talk", "people": ids},
            {"start_ms": half, "framing": "close-up", "action": "a reaction", "people": ids[:1]},
        ]}  # fmt: skip

    def zip(self) -> zipfile.ZipFile:
        r = self.http.get(f"/v1/drama-packages/{self.pid}/export")
        return zipfile.ZipFile(io.BytesIO(r.content))


def member(z: zipfile.ZipFile, suffix: str) -> bytes:
    [name] = [n for n in z.namelist() if n.endswith(suffix)]
    return z.read(name)


def rows_of(z: zipfile.ZipFile) -> list[dict]:
    return list(
        csv.DictReader(io.StringIO(member(z, "shot_keyframes/index.csv").decode("utf-8-sig")))
    )


def first_shot(b) -> str:
    return b.board()["shots"][0]["shot_id"]


def test_batch_plans_then_draws_and_checks_every_shot(tmp_path):
    with Board(tmp_path) as b:
        r = b.http.post(b.base, json={"plan": False})
        assert r.status_code == 202 and r.json()["started"] == []
        assert all("first: " in s["reason"] for s in r.json()["skipped"])
        b.design()
        r = b.http.post(b.base, json={"draws": 2})
        shots = b.board()["shots"]
        assert len(r.json()["planning"]) == len(shots) and r.json()["skipped"] == []
        board = b.wait()
        assert len(b.calls("shot_plan")) == len(shots)
        for s in board["shots"]:
            assert s["plan"]["source"] == "model" and len(s["sub_shots"]) == 1
            [take] = s["sub_shots"][0]["takes"]
            assert take["status"] == "done" and take["round"] == 1 and take["sub_shot"] == 1
            assert take["prompt_source"] == "model" and "<Picture 1>" in take["prompt"]
            assert all(d["check"]["passed"] for d in take["results"])
        check = b.calls("shot_check")[0]
        assert check.media[-1].label == "<Frame>"
        assert check.context["sub_shot"]["index"] == 1
        # A shot planned already is not planned again: it's drawn right away.
        r = b.http.post(b.base, json={"shot_ids": [shots[0]["shot_id"]], "draws": 1})
        assert r.json()["planning"] == [] and len(r.json()["started"]) == 1


def test_a_shot_cut_in_two_gets_a_keyframe_per_cut(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        r = b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        assert r.status_code == 202 and r.json()["source"] == "user"
        subs = b.shot(sid)["sub_shots"]
        assert subs[0]["timecode"] == "00:00.000" and len(subs) == 2
        assert subs[1]["missing"] == ["an approved keyframe for sub-shot 1"]
        assert b.http.post(f"{b.base}/{sid}", json={"sub_shot": 2}).status_code == 409
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        [one] = b.wait() and b.takes(sid)
        # Approving sub-shot 1 starts sub-shot 2, drawn from it.
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 1})
        b.wait()
        [two] = b.takes(sid, 2)
        assert two["references"][0]["ref_id"] == f"frame:{one['take_id']}:1"
        assert two["draws"] == 2 and two["status"] == "done"
        assert b.images.calls[-1][3][0].startswith(one["take_id"])
        writer = b.calls("shot_frames")[-1]
        assert writer.context["sub_shot"]["index"] == 2 and writer.context["sub_shot"]["of"] == 2
        assert writer.context["sub_shot"]["people_count"] == 1
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        # The export has both keyframes with the plan's timestamps.
        z = b.zip()
        rows = rows_of(z)
        assert [(r["shot_id"], r["sub_shot"]) for r in rows] == [(sid, "1"), (sid, "2")]
        assert rows[1]["timecode"] == subs[1]["timecode"]
        assert rows[1]["references"].startswith(f"<Picture 1>=shot_keyframes/{sid}_s1.")
        plans = json.loads(member(z, "shot_keyframes/sub_shots.json"))
        mine = next(p for p in plans if p["shot_id"] == sid)
        assert [s["keyframe"] for s in mine["sub_shots"]] == [rows[0]["file"], rows[1]["file"]]
        # Changing the second cut withdraws only its keyframe; the first stays.
        changed = b.two_cuts(shot)
        changed["sub_shots"][1]["action"] = "a different reaction"
        b.http.post(f"{b.base}/{sid}/plan", json=changed)
        subs = b.shot(sid)["sub_shots"]
        assert subs[0]["approved"] is not None and subs[1]["approved"] is None
        # Approving the old take is refused: the plan changed since it was drawn.
        r = b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        assert r.status_code == 409 and "plan changed" in r.json()["detail"]
        assert [r["sub_shot"] for r in rows_of(b.zip())] == ["1"]


class TwoCuts(FixtureModelClient):
    """The planner cuts every shot in two: everyone, then the first person close."""

    async def generate(self, request, output_model):
        if request.stage == "shot_plan":
            self.calls.append(request)
            shot = request.context["shot"]
            half = round(shot["duration_s"] / 2, 1)
            return PlanOutput(sub_shots=[
                PlannedSubShot(start_s=0, framing="wide", action="a", people=shot["people"]),
                PlannedSubShot(start_s=half, framing="close", action="b",
                               people=shot["people"][:1]),
            ], notes="two setups")  # fmt: skip
        return await super().generate(request, output_model)


def test_the_text_model_plans_the_cuts(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        r = b.http.post(f"{b.base}/{sid}/plan", json={})
        assert r.status_code == 202 and r.json()["status"] == "writing"
        b.wait()
        s = b.shot(sid)
        assert s["plan"]["source"] == "model" and s["plan"]["notes"] == "two setups"
        assert [len(x["people"]) for x in s["sub_shots"]] == [len(shot["people"]), 1]
        ctx = b.calls("shot_plan")[-1].context
        assert ctx["shot"]["people"] == [p["name"] for p in shot["people"]]


class BadPlanner(FixtureModelClient):
    """Plans cuts 0.3 s apart (too short); after `good` calls, plans a valid single cut."""

    good = 10**6

    async def generate(self, request, output_model):
        if request.stage == "shot_plan":
            self.good -= 1
            if self.good < 0:
                return PlanOutput(sub_shots=[
                    PlannedSubShot(start_s=0, framing="x", action="y"),
                    PlannedSubShot(start_s=0.3, framing="x", action="y"),
                ])  # fmt: skip
        return await super().generate(request, output_model)


def test_a_plan_that_breaks_the_rules_leaves_one_sub_shot(tmp_path):
    with Board(tmp_path, text=BadPlanner()) as b:
        b.design()
        sid = first_shot(b)
        b.text.good = 0  # every plan breaks the rules
        b.http.post(b.base, json={"shot_ids": [sid], "draws": 1})
        b.wait()
        s = b.shot(sid)
        assert s["plan"]["status"] == "failed" and "at least 1 s" in s["plan"]["error"]
        assert len(s["sub_shots"]) == 1 and s["sub_shots"][0]["takes"] == []
        # The single sub-shot can still be drawn.
        assert b.http.post(f"{b.base}/{sid}", json={"draws": 1}).status_code == 202


def test_a_failed_re_plan_keeps_the_plan_and_its_keyframes(tmp_path):
    with Board(tmp_path, text=BadPlanner()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [one] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0, "next": False})
        b.text.good = 0  # the next plan breaks the rules
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert s["plan"]["status"] == "failed" and s["plan"]["source"] == "user"
        assert len(s["sub_shots"]) == 2 and s["sub_shots"][0]["approved"] is not None


def test_the_batch_plans_only_shots_that_can_be_drawn(tmp_path):
    with Board(tmp_path) as b:
        r = b.http.post(b.base, json={})  # no design picks yet
        assert r.json()["planning"] == [] and r.json()["started"] == []
        assert all("first: " in s["reason"] for s in r.json()["skipped"])
        assert b.calls("shot_plan") == []


def test_approving_starts_the_next_sub_shot_even_after_a_re_plan(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [one] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
        [old] = b.wait() and b.takes(sid, 2)
        changed = b.two_cuts(shot)
        changed["sub_shots"][1]["framing"] = "extreme close-up"
        b.http.post(f"{b.base}/{sid}/plan", json=changed)
        assert b.takes(sid, 2) == []  # the old take was for the old sub-shot 2
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
        [new] = b.wait() and b.takes(sid, 2)
        assert new["take_id"] != old["take_id"] and new["spec"]["framing"] == "extreme close-up"


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


def test_a_failed_check_is_revised_in_a_new_round_with_the_same_seeds(tmp_path):
    with Board(tmp_path, text=FailsFirstRound()) as b:
        b.design()
        sid = first_shot(b)
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 2, "seeds": [11, 22]})
        assert r.status_code == 202, r.text
        one, two = b.wait() and b.takes(sid)
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
        assert b.calls("shot_frames")[-1].context["fixes"] == two["fixes"]


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
        takes = b.takes(sid)
        assert [t["round"] for t in takes] == [1, 2, 3]
        last = takes[-1]
        r = b.http.post(f"/v1/shot-takes/{last['take_id']}/approve", json={"n": 0})
        assert r.status_code == 200 and r.json()["approved"] == 0
        z = b.zip()
        [row] = rows_of(z)
        assert row["shot_id"] == sid and row["sub_shot"] == "1" and row["check"] == "failed"
        assert row["timecode"] == "00:00.000" and "a third person" in row["failed_items"]
        assert row["round"] == "3"
        assert any(n.endswith(f"shot_keyframes/{sid}_s1.png") for n in z.namelist())


def test_directions_and_deleting_an_approved_keyframe(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        b.wait()
        b.http.post(f"{b.base}/{sid}", json={"draws": 1, "direction": "tighter, on Frank"})
        first, second = b.wait() and b.takes(sid)
        assert second["direction"] == "tighter, on Frank"
        assert b.calls("shot_frames")[-1].context["direction"] == "tighter, on Frank"
        b.http.post(f"/v1/shot-takes/{first['take_id']}/approve", json={"n": 1, "next": False})
        assert b.takes(sid, 2) == []  # next=False: sub-shot 2 not started
        b.http.post(f"/v1/shot-takes/{second['take_id']}/approve", json={"n": 0})
        first, second = b.takes(sid)
        assert first["approved"] is None and second["approved"] == 0  # one per sub-shot
        [two] = b.wait() and b.takes(sid, 2)  # started by the approval
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        # A keyframe drawn from another sub-shot 1 draw can't be approved.
        b.http.post(f"/v1/shot-takes/{first['take_id']}/approve", json={"n": 0})
        r = b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        assert r.status_code == 409 and "isn't approved now" in r.json()["detail"]
        b.http.post(f"/v1/shot-takes/{second['take_id']}/approve", json={"n": 0})
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        # Deleting the approved sub-shot 1 take withdraws sub-shot 2 drawn from it.
        assert b.http.delete(f"/v1/shot-takes/{second['take_id']}").status_code == 204
        assert [s["approved"] for s in b.shot(sid)["sub_shots"]] == [None, None]


def test_your_own_prompt_is_checked_and_not_rewritten(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": "<Picture 30> is <Subject 30>."})
        assert r.status_code == 422 and "<Picture 30>" in r.json()["detail"]
        mine = "The person in <Picture 1> is <Subject 1>. The target image is a still frame."
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": mine, "draws": 1})
        assert r.status_code == 202 and r.json()["auto"] is False
        take = b.wait() and b.takes(sid)[-1]
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
        [take] = b.takes(sid)  # no next round: nothing to revise against
        [d] = take["results"]
        assert d["check"] is None and "judge unavailable" in d["check_error"]
        b.text.__class__ = FixtureModelClient  # the judge is back
        r = b.http.post(f"/v1/shot-takes/{take['take_id']}/check")
        assert r.status_code == 202 and r.json()["status"] == "checking"
        [take] = b.wait() and b.takes(sid)
        assert take["status"] == "done" and take["results"][0]["check"]["passed"]
        assert len(b.calls("shot_check")) == 1  # (the failed call was never recorded)
        b.http.post(f"/v1/shot-takes/{take['take_id']}/check")  # all checked: all again
        b.wait()
        assert len(b.calls("shot_check")) == 2


def test_an_intimate_scene_is_never_drawn_from_a_likeness(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
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
        [take] = b.takes(sid)
        assert take["status"] == "failed" and take["refused"] and take["likeness"]
        assert "content rule 3" in take["error"]
        assert len(b.images.calls) == drawn  # nothing was drawn
        r = b.http.post(f"/v1/shot-takes/{take['take_id']}/retry")
        assert r.status_code == 409 and "content rules" in r.json()["detail"]


def test_batch_with_no_shots_starts_nothing(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        r = b.http.post(b.base, json={"shot_ids": []})
        assert r.json() == {"started": [], "planning": [], "skipped": []}
        assert b.board()["busy"] == 0


def test_sqlite_keeps_plans(tmp_path, package):
    repo = SqliteRepository(tmp_path / "t.db")
    plan = two_cuts(package, busiest(package))
    repo.save_shot_plan(plan)
    got = repo.get_shot_plan(plan.package_id, plan.revision_id, plan.shot_id)
    assert got == plan and repo.list_shot_plans(plan.package_id, plan.revision_id) == [plan]
    plan.status = "writing"
    repo.save_shot_plan(plan)
    assert repo.fail_unfinished_shot_plans("restarted") == 1
    assert repo.get_shot_plan(plan.package_id, plan.revision_id, plan.shot_id).status == "failed"
