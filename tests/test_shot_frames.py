"""Shot keyframes: a sub-shot plan per shot and a checked keyframe per sub-shot."""

import asyncio
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
    PlanProblems,
    PromptVersion,
    ShotFrameRunner,
    ShotPicks,
    ShotPlan,
    ShotTake,
    check_plan,
    clip_ms,
    current_sub,
    make_sub_shots,
    next_round,
    picture_sentences,
    plan_of,
    plan_problems,
    plan_take,
    planned_sub_shots,
    prompt_findings,
    score,
    shot_lines,
    shot_of,
    shot_references,
    single_plan,
    spread,
    stale_approvals,
    sub_shot_references,
    template_prompt,
    writer_instructions,
)
from drama_agent.shots.staleness import KF_BEFORE_CHANGED, KF_BEFORE_MISSING, outdated_keyframes
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
    """The shot as a wide on everyone, then (after its lines) a close-up on its first
    person, who keeps their side of the frame."""
    half = shot.duration_ms // 2
    sides = spread(shot.character_ids)
    first = shot.character_ids[0]
    subs = make_sub_shots(pkg, shot, [
        {"start_ms": 0, "framing": "wide", "action": "they talk", "people": shot.character_ids,
         "placement": sides},
        {"start_ms": half, "framing": "close-up", "camera": "still", "action": "a reaction",
         "people": [first], "placement": {first: sides[first]}},
    ])  # fmt: skip
    return ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id,
                    shot_id=shot.shot_id, source="user", sub_shots=subs)  # fmt: skip


def finished(take: ShotTake, approved: int | None = 0) -> ShotTake:
    take.status, take.results, take.approved = "done", [FrameDraw(n=0, seed=1)], approved
    return take


def test_references_are_looks_then_set_then_props_then_faces(package, monkeypatch):
    images = done_images(package)
    shot = busiest(package)
    # Six pictures unless set: the first six of the same order.
    six, _ = shot_references(package, images, shot)
    monkeypatch.setenv("DRAMA_KEYFRAME_MAX_REFERENCES", "9")
    refs, missing = shot_references(package, images, shot)
    assert len(six) <= 6 and six == refs[: len(six)]
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
    shot = busiest(package)  # 15 s; one line at 1.0-6.2 s
    ids = shot.character_ids
    sides = spread(ids)
    wide = {"framing": "wide", "action": "x", "people": ids, "placement": sides}
    close = {"framing": "close-up", "action": "y", "people": ids[:1],
             "placement": {ids[0]: sides[ids[0]]}}  # fmt: skip
    two = make_sub_shots(package, shot, [{**wide, "start_ms": 0}, {**close, "start_ms": 7000}])
    assert two[1].timecode == "00:07.000" and two[0].end_ms == 7000
    assert two[1].end_ms == 15000
    bad = [
        ([{**wide, "start_ms": 500}], "starts at 0"),
        ([{**wide, "start_ms": 0}, {**close, "start_ms": 300}], "at least 1 s"),
        ([{**wide, "start_ms": 0}] * 6, "1 to 5"),
        ([{**wide, "start_ms": 0, "people": ["ch_nobody"]}], "isn't in the shot"),
        ([{**wide, "start_ms": 0, "framing": " "}], "needs a framing"),
        ([{**wide, "start_ms": 0}, {**close, "start_ms": 3000}], "middle of"),
        ([{**wide, "start_ms": 0}, {**wide, "start_ms": 7000}], "same framing"),
        ([{**wide, "start_ms": 0, "placement": {}}], "where is"),
        ([{**wide, "start_ms": 0, "people": [], "placement": {}}], "neither on screen"),
    ]
    flipped = "right" if sides[ids[0]] == "left" else "left"
    bad.append(([{**wide, "start_ms": 0},
                 {**close, "start_ms": 7000, "placement": {ids[0]: flipped}}], "180-degree"))  # fmt: skip
    for rows, message in bad:
        with pytest.raises(ValueError, match=message):
            make_sub_shots(package, shot, rows)
    with pytest.raises(ValueError, match="whole seconds"):
        make_sub_shots(package, shot, [{**wide, "start_ms": 0}], clip=16000)
    # A line's speaker off screen while someone else is in frame is refused (H3 would put
    # the line on the face it sees); the same single with the speaker in it is fine.
    speaker = shot_lines(package, shot)[0]["speaker_id"]
    other = next(i for i in ids if i != speaker)
    side = lambda i: "left" if sides[i] == "left" else "right"  # centre: off to the right
    off = {**close, "start_ms": 0, "people": [other], "placement": {other: sides[other]},
           "off_screen": {i: side(i) for i in ids if i != other}}  # fmt: skip
    with pytest.raises(ValueError, match="an off-screen line"):
        make_sub_shots(package, shot, [off, {**wide, "start_ms": 7000}])
    on = {**off, "people": [speaker], "placement": {speaker: sides[speaker]},
          "off_screen": {i: side(i) for i in ids if i != speaker}}  # fmt: skip
    assert make_sub_shots(package, shot, [on, {**wide, "start_ms": 7000}])


def test_the_clip_is_whole_seconds_and_holds_every_line(package):
    shot = package.shot_list[0].model_copy(update={"duration_ms": 8300})
    assert clip_ms(shot) == 9000  # rounded up, so a line timed at 8.2 s is in the clip
    assert clip_ms(shot.model_copy(update={"duration_ms": 2500})) == 4000
    assert clip_ms(shot.model_copy(update={"duration_ms": 20000})) == 15000


def test_the_planner_names_become_character_ids(package):
    shot = busiest(package)
    names = {c.character_id: c.name for c in package.story_bible.characters}
    speaker = shot_lines(package, shot)[0]["speaker_id"]  # in frame for the line (0-6.2 s)
    first = names[speaker]
    others = {names[c]: "right" for c in shot.character_ids if c != speaker}
    out = PlanOutput(sub_shots=[
        PlannedSubShot(start_s=0, framing="wide", action="a", people=[first.upper()],
                       placement={first: "left"}, off_screen=others),
        PlannedSubShot(start_s=7.0, framing="close", action="b", people=[first],
                       placement={first.lower(): "Left"}, off_screen=others),
    ])  # fmt: skip
    subs = planned_sub_shots(package, shot, out)
    assert [s.people for s in subs] == [[speaker]] * 2
    assert subs[1].start_ms == 7000 and subs[1].placement == {speaker: "left"}
    # Someone who isn't in the shot (the action mentions them) is left out, not refused.
    out.sub_shots[1].people = [first, "Nobody Here"]
    assert [s.people for s in planned_sub_shots(package, shot, out)] == [[speaker]] * 2
    out.sub_shots[1].people = [first]
    # Small slips are forgiven: a name with a surname or title, a first cut just after 0.
    first_name = first.split()[0]
    out.sub_shots[0].start_s = 0.2
    out.sub_shots[1].people = [f"Ms. {first_name} Somebody"]
    subs = planned_sub_shots(package, shot, out)
    assert subs[0].start_ms == 0 and subs[1].people == [speaker]


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
def test_approval_is_one_draw_per_cut_and_moves_nothing_else(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    images = done_images(package)
    shot = busiest(package)
    plan = two_cuts(package, shot)
    one = finished(plan_take(package, images, [], plan))
    two = finished(plan_take(package, images, [one], plan, sub_shot=2))
    other = finished(plan_take(package, images, [one, two], plan), approved=None)
    for t in (one, two, other):
        repo.save_shot_take(t)
        if t.approved is not None:
            repo.approve_shot_draw(t.take_id, 0)
    assert repo.get_shot_take(two.take_id).approved == 0
    repo.approve_shot_draw(other.take_id, 0)  # another cut 1 keyframe: cut 1 switches
    assert repo.get_shot_take(one.take_id).approved is None
    assert repo.get_shot_take(other.take_id).approved == 0
    # Cut 2 stays approved, drawn from a keyframe that isn't the approved one: out of date.
    takes = repo.list_shot_takes(package.package_id, package.revision_id)
    assert repo.get_shot_take(two.take_id).approved == 0
    assert outdated_keyframes(package, images, takes, [plan]) == {two.take_id: [KF_BEFORE_CHANGED]}
    repo.approve_shot_draw(other.take_id, None)  # withdrawn: cut 2 still holds
    takes = repo.list_shot_takes(package.package_id, package.revision_id)
    assert repo.get_shot_take(two.take_id).approved == 0
    assert outdated_keyframes(package, images, takes, [plan]) == {two.take_id: [KF_BEFORE_MISSING]}
    repo.approve_shot_draw(one.take_id, 0)  # the keyframe it was drawn from: up to date
    takes = repo.list_shot_takes(package.package_id, package.revision_id)
    assert outdated_keyframes(package, images, takes, [plan]) == {}


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
    # Sub-shot 2 is not blocked by the other person's missing pictures. (Drawing it would be:
    # the keyframe it continues from was drawn from pictures this list no longer has.)
    refs, missing = sub_shot_references(package, images, [one], shot, plan.sub(2))
    assert missing == []
    assert {r.entity_id for r in refs if r.kind in ("look", "face")} == {shot.character_ids[0]}


def test_the_picture_cap_is_applied_per_sub_shot(monkeypatch):
    def ref(kind, who):
        return FrameRef(ref_id=f"img_{kind}_{who}", role=who, kind=kind, entity_id=who)

    crowd = [f"ch_{i}" for i in range(7)]
    picks = ShotPicks(
        looks=[ref("look", c) for c in crowd], faces=[ref("face", c) for c in crowd],
        sets=[ref("set", "loc")], props=[ref("prop", "p1"), ref("prop", "p2")], missing=[],
    )  # fmt: skip
    # Six pictures a draw unless set (each costs the image server time and memory).
    assert [r.kind for r in picks.refs()] == ["look"] * 6
    prev = FrameRef(ref_id="frame:t:0", role="before", kind="previous_frame")
    assert [r.kind for r in picks.refs({"ch_6"}, prev)] == [
        "previous_frame", "look", "set", "prop", "prop", "face"]  # fmt: skip
    # Up to the model's nine when asked for, and never more.
    from drama_agent.shots.common import keyframe_max_references

    for value, most in (("9", 9), ("12", 9), ("0", 1), ("many", 6)):
        monkeypatch.setenv("DRAMA_KEYFRAME_MAX_REFERENCES", value)
        assert keyframe_max_references() == most
    monkeypatch.setenv("DRAMA_KEYFRAME_MAX_REFERENCES", "9")
    everyone = picks.refs()
    assert len(everyone) == 9 and not any(r.kind == "face" for r in everyone)
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
        # The second keyframe provider: a stand-in for Nano Banana (eight pictures at most).
        self.banana = FixtureImageClient(model_id="fixture-nano-banana", max_references=8)
        self.media = MediaStore(tmp_path)
        self.repo = InMemoryRepository()
        self.app = create_app(
            repo=self.repo, client=self.text, image_client=self.images, media=self.media,
            poll_s=0.01, keyframe_providers={"nano-banana": self.banana},
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
        sides = spread(ids)
        return {"sub_shots": [
            {"start_ms": 0, "framing": "wide", "action": "they talk", "people": ids,
             "placement": sides},
            {"start_ms": half, "framing": "close-up", "action": "a reaction", "people": ids[:1],
             "placement": {ids[0]: sides[ids[0]]}},
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
        spec = json.loads(member(z, "shot_keyframes/sub_shots.json"))
        assert spec["spec_version"] == 1
        mine = next(p for p in spec["shots"] if p["shot_id"] == sid)
        assert mine["clean"] and mine["problems"] == [] and mine["clip_ms"] == shot["clip_ms"]
        assert [s["keyframe"] for s in mine["sub_shots"]] == [rows[0]["file"], rows[1]["file"]]
        first, second = mine["sub_shots"]
        assert set(first["placement"]) == set(first["people"]) and second["placement"]
        assert all(x["where"] == "on screen" for x in first["lines"])
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
            half = round(request.context["clip_s"] / 2, 1)
            sides = spread(shot["people"])
            first = shot["people"][0]
            return PlanOutput(sub_shots=[
                PlannedSubShot(start_s=0, framing="wide", action="a", people=shot["people"],
                               placement=sides),
                PlannedSubShot(start_s=half, framing="close", action="b", people=[first],
                               placement={first: sides[first]}),
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
        # Approval is a plain switch: another cut 1 keyframe approved, cut 2 (drawn from
        # the old one) stays approved and is marked out of date; back, it is up to date.
        b.http.post(f"/v1/shot-takes/{first['take_id']}/approve", json={"n": 0})
        subs = b.shot(sid)["sub_shots"]
        assert subs[1]["approved"] == [two["take_id"], 0] and subs[1]["outdated"] == [
            KF_BEFORE_CHANGED
        ]
        b.http.post(f"/v1/shot-takes/{second['take_id']}/approve", json={"n": 0})
        assert b.shot(sid)["sub_shots"][1]["outdated"] == []
        # Deleting the approved cut 1 take leaves cut 2 approved, out of date.
        assert b.http.delete(f"/v1/shot-takes/{second['take_id']}").status_code == 204
        subs = b.shot(sid)["sub_shots"]
        assert [s["approved"] for s in subs] == [None, [two["take_id"], 0]]
        assert subs[1]["outdated"] == [KF_BEFORE_MISSING]


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


# ---------------------------------------------------------------------------
# H3 prompt rules and prompt versions

PASTED = (
    "The woman in <Picture 1> is <Subject 1>. The man in <Picture 2> is <Subject 2>. "
    "The apartment in <Picture 3> is <Subject 3>. The woman's grooming in <Picture 4> is "
    "<Subject 4>. The target image is a still frame from a live-action short drama. Both are "
    "fully clothed in a non-intimate morning conversation. <Subject 1> is partially preserved. "
    "<Subject 2> is partially preserved. <Subject 3> is partially preserved. <Subject 4> serves "
    "as a weak reference for natural makeup only, without transferring facial features. The bed "
    "is unoccupied, with nobody beneath it. Frame both faces clearly and retain the foreground "
    "floor. No other lettering is added."
)


def face_take(package):
    """A take on the busiest shot: looks, the set, props and faces."""
    shot = busiest(package)
    return plan_take(package, done_images(package), [], single_plan(package, shot))


def test_the_rule_check_finds_what_was_wrong_with_the_pasted_prompt(package):
    take = face_take(package)
    refs = take.references[:4]
    refs[3] = refs[3].model_copy(update={"kind": "face"})
    found = " | ".join(prompt_findings(PASTED, refs))
    assert "uses negation (nobody, without)" in found
    assert "gives instructions (frame)" in found
    assert "story or intent words" in found and "non-intimate" in found
    assert "weakens <Subject 4>, a face picture" in found


def test_the_template_follows_the_rules_and_transfers_faces(package):
    take = face_take(package)
    prompt = template_prompt(package, take)
    assert prompt_findings(prompt, take.references) == []
    sentences = picture_sentences(package, take)
    faces = [x for x, r in zip(sentences, take.references, strict=True) if r.kind == "face"]
    assert faces and all("supplies the exact facial features transferred to" in x["relationship"]
                         for x in faces)  # fmt: skip
    # A later sub-shot's template too (previous keyframe first).
    plan = two_cuts(package, busiest(package))
    one = finished(plan_take(package, done_images(package), [], plan))
    two = plan_take(package, done_images(package), [one], plan, sub_shot=2)
    assert prompt_findings(template_prompt(package, two), two.references) == []


class SloppyThenFixed(FixtureModelClient):
    """The writer's first draft breaks the rules; its repair follows them."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_frames" and request.attempt == 0:
            out.prompt = out.prompt.replace("The target image", "Keep the faces. The target image")
        return out


def test_a_prompt_that_breaks_the_rules_is_repaired_once(tmp_path):
    with Board(tmp_path, text=SloppyThenFixed()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        first, second = b.calls("shot_frames")
        assert first.context["pictures"][0]["define"].endswith("is <Subject 1>.")
        assert any("instructions (keep)" in f for f in second.context["fixes"])
        assert take["prompt_source"] == "model" and take["warnings"] == []
        assert "Keep the faces" not in take["prompt"] and take["prompt_version"] == 1


class AlwaysSloppy(FixtureModelClient):
    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_frames":
            out.prompt = out.prompt.replace("The target image", "Nobody else. The target image")
        return out


def test_a_prompt_still_off_after_repair_is_kept_with_warnings(tmp_path):
    with Board(tmp_path, text=AlwaysSloppy()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        assert take["prompt_source"] == "model" and "Nobody else" in take["prompt"]
        assert any("negation" in w for w in take["warnings"])
        [version] = b.shot(sid)["sub_shots"][0]["versions"]
        assert version["v"] == 1 and version["findings"] == take["warnings"]


def test_prompt_versions_can_be_edited_compared_and_drawn(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        [first] = b.wait() and b.takes(sid)
        [v1] = b.shot(sid)["sub_shots"][0]["versions"]
        assert v1["v"] == 1 and v1["source"] == "model" and v1["take_id"] == first["take_id"]
        # An edit is saved as v2, with its findings; nothing is drawn.
        edited = v1["prompt"].replace("No other lettering", "Warm lamp light. No other lettering")
        r = b.http.post(f"{b.base}/{sid}/prompts",
                        json={"prompt": edited, "based_on": 1, "note": "warmer"})  # fmt: skip
        assert r.status_code == 201, r.text
        v2 = r.json()
        assert (v2["v"], v2["source"], v2["based_on"], v2["note"]) == (2, "user", 1, "warmer")
        assert len(b.takes(sid)) == 1
        bad = b.http.post(f"{b.base}/{sid}/prompts", json={"prompt": "<Picture 30> is it."})
        assert bad.status_code == 422 and "<Picture 30>" in bad.json()["detail"]
        sloppy = b.http.post(f"{b.base}/{sid}/prompts",
                             json={"prompt": edited.replace("Warm", "Do not dim the")})  # fmt: skip
        assert sloppy.status_code == 201 and sloppy.json()["findings"]
        # Drawing v2 uses the last take's seeds, so the draws compare one for one.
        r = b.http.post(f"{b.base}/{sid}", json={"prompt_version": 2})
        assert r.status_code == 202, r.text
        take = r.json()
        assert take["prompt"] == edited and take["prompt_version"] == 2 and not take["auto"]
        assert take["seeds"] == first["seeds"] and take["prompt_source"] == "user"
        b.wait()
        assert [v["v"] for v in b.shot(sid)["sub_shots"][0]["versions"]] == [1, 2, 3]
        assert b.http.post(f"{b.base}/{sid}", json={"prompt_version": 9}).status_code == 404
        # A new plan for the sub-shot starts its own history.
        shot = b.shot(sid)
        if shot["duration_ms"] >= 2000 and shot["people"]:
            b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
            assert b.shot(sid)["sub_shots"][0]["versions"] == []
            r = b.http.post(f"{b.base}/{sid}", json={"prompt_version": 2})
            assert r.status_code == 409 and "older plan" in r.json()["detail"]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_prompt_versions_are_numbered_per_sub_shot(kind, package, tmp_path):
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    shot = busiest(package)
    plan = two_cuts(package, shot)

    def add(sub):
        return repo.add_prompt_version(PromptVersion(
            package_id=package.package_id, revision_id=package.revision_id,
            shot_id=shot.shot_id, sub_shot=sub, spec=plan.sub(sub), prompt="p", source="user",
        ))  # fmt: skip

    assert [add(1).v, add(1).v, add(2).v, add(1).v] == [1, 2, 1, 3]
    got = repo.list_prompt_versions(package.package_id, package.revision_id)
    assert [(v.sub_shot, v.v) for v in got] == [(1, 1), (1, 2), (1, 3), (2, 1)]


class RoundTwoSloppy(FixtureModelClient):
    """The judge fails round 1; round 2's first draft breaks a rule; its repair is clean."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_check" and "FIXED" not in request.context["prompt"]:
            out.items.append(CheckItem(item="Frank: wardrobe", kind="wardrobe", passed=False,
                                       finding="the coat is blue"))  # fmt: skip
        if request.stage == "shot_frames" and request.context["fixes"]:
            out.prompt += " FIXED"
            if request.attempt == 0:
                out.prompt = out.prompt.replace("The target image", "Keep it. The target image")
        return out


def test_the_repair_keeps_the_rounds_own_fixes(tmp_path):
    with Board(tmp_path, text=RoundTwoSloppy()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        _, two = b.wait() and b.takes(sid)
        repair = b.calls("shot_frames")[-1]
        assert repair.attempt == 1
        assert "Frank: wardrobe: the coat is blue" in repair.context["fixes"]
        assert any(f.startswith("The rule check:") for f in repair.context["fixes"])
        assert two["warnings"] == [] and "Keep it" not in two["prompt"]


class SoftThenBroken(FixtureModelClient):
    """The first draft has a soft finding; the repair cites a picture that isn't sent."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_frames":
            if request.attempt == 0:
                out.prompt = out.prompt.replace("The target image", "Nobody else. The target image")
            else:
                out.prompt += " <Picture 30> is <Subject 30>."
        return out


def test_a_usable_first_draft_survives_a_broken_repair(tmp_path):
    with Board(tmp_path, text=SoftThenBroken()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        assert take["prompt_source"] == "model" and "Nobody else" in take["prompt"]
        assert "<Picture 30>" not in take["prompt"]
        assert any("negation" in w for w in take["warnings"])


class StoryWordsOnly(FixtureModelClient):
    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_frames":
            out.prompt = out.prompt.replace(
                "The target image", "A platonic moment. The target image"
            )
        return out


def test_story_words_alone_dont_cost_a_second_call(tmp_path):
    with Board(tmp_path, text=StoryWordsOnly()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        assert len(b.calls("shot_frames")) == 1
        assert any("story or intent" in w for w in take["warnings"])


def test_ordinary_words_are_not_flagged():
    text = ("Lila in <Picture 1> is <Subject 1>. The target image is a still frame of a quiet "
            "conversation; the intimate lamp light shows her face. <Subject 1> is partially "
            "preserved. Use of warm tones. No other lettering is added.")  # fmt: skip
    ref = FrameRef(ref_id="img_1", role="Lila", kind="look", entity_id="ch_lila")
    assert prompt_findings(text, [ref]) == []


def test_save_and_draw_keeps_lineage_and_saves_nothing_when_refused(tmp_path):
    with Board(tmp_path) as b:
        sid = first_shot(b)
        r = b.http.post(f"{b.base}/{sid}", json={"prompt": "x", "draws": 1})
        assert r.status_code == 409 and "first: " in r.json()["detail"]  # no picks yet
        assert b.shot(sid)["sub_shots"][0]["versions"] == []
        b.design()
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        [v1] = b.shot(sid)["sub_shots"][0]["versions"]
        edited = v1["prompt"].replace(
            "No other lettering", "Rain streaks the window. No other lettering"
        )
        r = b.http.post(f"{b.base}/{sid}",
                        json={"prompt": edited, "based_on": 1, "note": "rain", "draws": 1})  # fmt: skip
        assert r.status_code == 202, r.text
        b.wait()
        v2 = b.shot(sid)["sub_shots"][0]["versions"][-1]
        assert (v2["v"], v2["based_on"], v2["note"]) == (2, 1, "rain")
        assert b.shot(sid)["sub_shots"][0]["next_version"] == 3
        # After a re-plan the list starts afresh, but numbering continues.
        shot = b.shot(sid)
        if shot["duration_ms"] >= 2000 and shot["people"]:
            b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
            sub = b.shot(sid)["sub_shots"][0]
            assert sub["versions"] == [] and sub["next_version"] == 3


def test_a_definition_never_loses_its_subject(package):
    take = face_take(package)
    take.references = [r.model_copy(update={"entity_id": "ch_unknown"}) if r.kind in ("look", "face")
                       else r for r in take.references]  # fmt: skip
    for x in picture_sentences(package, take):
        assert not x["define"].startswith(" ") and x["define"][0].isupper()


class CutsMidLine(FixtureModelClient):
    """The planner first cuts in the middle of the shot's line; told so, it cuts after it."""

    async def generate(self, request, output_model):
        if request.stage != "shot_plan":
            return await super().generate(request, output_model)
        self.calls.append(request)
        ctx = request.context
        people = ctx["shot"]["people"]
        line = ctx["lines"][0]
        cut = (
            (line["start_s"] + line["end_s"]) / 2 if "problems" not in ctx else line["end_s"] + 0.5
        )
        sides = spread(people)
        return PlanOutput(sub_shots=[
            PlannedSubShot(start_s=0, framing="wide", action="a", people=people, placement=sides),
            PlannedSubShot(start_s=cut, framing="close", action="b", people=people[:1],
                           placement={people[0]: sides[people[0]]}),
        ])  # fmt: skip


def test_a_plan_that_cuts_mid_line_is_repaired(tmp_path):
    with Board(tmp_path, text=CutsMidLine()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        # The plan reader moves the cut to the line's end itself: one planner call.
        [first] = b.calls("shot_plan")
        assert first.context["clip_s"] == shot["clip_ms"] // 1000 and first.context["lines"]
        assert s["plan"]["status"] == "done" and s["plan"]["problems"] == []
        line = first.context["lines"][0]
        assert s["sub_shots"][1]["start_ms"] == line["end_s"] * 1000


def test_an_unclean_stored_plan_is_flagged_on_the_board_and_in_the_spec(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        pkg = b.repo.get_package(b.pid)
        sub = single_plan(pkg, shot_of(pkg, sid)).sub_shots[0]  # no frame positions
        b.repo.save_shot_plan(ShotPlan(package_id=pkg.package_id, revision_id=pkg.revision_id,
                                       shot_id=sid, source="user", sub_shots=[sub]))  # fmt: skip
        problems = b.shot(sid)["plan"]["problems"]
        assert problems and all("where is" in p for p in problems)
        spec = json.loads(member(b.zip(), "shot_keyframes/sub_shots.json"))
        mine = next(p for p in spec["shots"] if p["shot_id"] == sid)
        assert mine["clean"] is False and mine["problems"] == problems


class AlwaysNoSides(CutsMidLine):
    """Never says where the second cut's person is in the frame, even when told (a slip
    the plan reader can't settle from the storyboard)."""

    async def generate(self, request, output_model):
        if request.stage == "shot_plan":
            request.context.pop("problems", None)
        out = await super().generate(request, output_model)
        if request.stage == "shot_plan":
            out.sub_shots[1].placement = {}
        return out


def test_a_model_plan_still_not_clean_is_kept_and_flagged(tmp_path):
    with Board(tmp_path, text=AlwaysNoSides()) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert len(b.calls("shot_plan")) == 2
        assert s["plan"]["source"] == "model" and s["plan"]["status"] == "done"
        assert len(s["sub_shots"]) == 2 and any("where is" in p for p in s["plan"]["problems"])
        spec = json.loads(member(b.zip(), "shot_keyframes/sub_shots.json"))
        assert next(p for p in spec["shots"] if p["shot_id"] == sid)["clean"] is False


def test_the_clip_length_asked_for_is_planned(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={"clip_s": 10})
        s = b.wait() and b.shot(sid)
        assert b.calls("shot_plan")[-1].context["clip_s"] == 10
        assert s["clip_ms"] == 10000 and s["sub_shots"][-1]["end_ms"] == 10000


def test_more_rules_of_a_clean_spec(package):
    shot = busiest(package)  # 15 s; one line at 1.0-6.2 s
    ids = shot.character_ids
    sides = spread(ids)
    wide = {"start_ms": 0, "framing": "wide", "action": "x", "people": ids, "placement": sides}
    # A line running past the clip's end.
    with pytest.raises(PlanProblems) as err:
        make_sub_shots(package, shot, [wide], clip=5000)
    assert any("past the clip's end" in p for p in err.value.problems)
    # A frame position for someone who isn't on screen.
    close = {**wide, "people": ids[:1]}
    with pytest.raises(PlanProblems, match="isn't on screen in it"):
        make_sub_shots(package, shot, [close])
    # Not strict: the problems are left for the caller to flag.
    assert make_sub_shots(package, shot, [wide], clip=5000, strict=False)


def test_a_shot_too_long_for_h3_is_flagged_but_still_planned(package):
    shot = busiest(package).model_copy(update={"duration_ms": 20000})
    plan = single_plan(package, shot)
    assert plan.sub_shots[0].end_ms == 15000
    [problem] = plan_problems(package, shot, plan)
    assert "longer than H3's 15 s clips: split it" in problem
    ids = shot.character_ids
    assert make_sub_shots(package, shot, [{"start_ms": 0, "framing": "wide", "action": "x",
                                           "people": ids, "placement": spread(ids)}])  # fmt: skip


def test_a_keyframe_from_before_whole_second_clips_still_counts(package):
    shot = package.shot_list[0].model_copy(update={"duration_ms": 7500})
    plan = single_plan(package, shot)  # now 8 s
    take = finished(
        plan_take(package, done_images(package), [], single_plan(package, package.shot_list[0]))
    )
    take.shot_id = shot.shot_id
    take.spec = plan.sub_shots[0].model_copy(update={"end_ms": 7500})  # drawn for 7.5 s
    assert current_sub(package, shot, plan, take) == plan.sub_shots[0]
    other = plan.sub_shots[0].model_copy(update={"end_ms": 7500, "framing": "else"})
    take.spec = other
    assert current_sub(package, shot, plan, take) is None


# ---------------------------------------------------------------------------
# Seen live on Heartbeats ep1 shot 1 (2026-11-17)


def two_speakers(package):
    """The busiest shot with a second line, by another person on screen, after the first."""
    from drama_agent.models.storyboard import DialogueCue

    shot = busiest(package)
    first = shot_lines(package, shot)[0]
    other = next(
        bt.dialogue
        for ep in package.episodes for sc in ep.scenes for bt in sc.beats
        if bt.dialogue and bt.dialogue.character_id in shot.character_ids
        and bt.dialogue.character_id != first["speaker_id"]
    )  # fmt: skip
    cue = DialogueCue(dialogue_id=other.dialogue_id, start_ms=shot.start_ms + 8000,
                      end_ms=shot.start_ms + 11000)  # fmt: skip
    return shot.model_copy(update={"dialogue_cues": [*shot.dialogue_cues, cue]})


def test_dialogue_between_two_people_in_one_cut_is_flagged_but_may_be_kept(package):
    shot = two_speakers(package)
    plan = single_plan(package, shot)
    [problem] = plan_problems(package, shot, plan)
    assert problem.startswith("dialogue held in one cut: two people speak in this 15 s clip")
    assert "a cut at" in problem and 'note "hold the two-shot" in the storyboard' in problem
    # A person may hold the two-shot on purpose: their save isn't refused for it.
    ids = shot.character_ids
    row = {"start_ms": 0, "framing": "wide", "action": "x", "people": ids, "placement": spread(ids)}
    assert any(p.startswith("dialogue held in one cut")
               for p in check_plan(package, shot, make_sub_shots(package, shot, [row])))  # fmt: skip


def test_a_storyboard_that_holds_the_two_shot_is_not_flagged(package):
    shot = two_speakers(package).model_copy(update={"generation_notes": "Hold the two-shot."})
    assert plan_problems(package, shot, single_plan(package, shot)) == []


def test_coverage_needs_a_place_to_cut_and_lines_inside_the_clip(package):
    from drama_agent.shot_frames import coverage_cut

    a = {"speaker_id": "a", "start_ms": 200, "end_ms": 1100}
    b = {"speaker_id": "b", "start_ms": 1300, "end_ms": 7600}
    assert coverage_cut([a, b], 8000) == 1200  # between the lines, 1 s from the start
    assert coverage_cut([a, {**b, "speaker_id": "a"}], 8000) is None  # one speaker
    assert coverage_cut([{**a, "end_ms": 400}, {**b, "start_ms": 600}], 8000) is None  # too early
    # A second speaker whose line starts after the clip ends doesn't count.
    shot = two_speakers(package)
    late = shot.model_copy(update={"dialogue_cues": [
        *shot.dialogue_cues[:-1],
        shot.dialogue_cues[-1].model_copy(update={"start_ms": shot.start_ms + 15500,
                                                  "end_ms": shot.start_ms + 16000}),
    ]})  # fmt: skip
    assert not any(
        p.startswith("dialogue held")
        for p in plan_problems(package, late, single_plan(package, late))
    )


def test_advisory_problems_are_matched_by_their_start():
    from drama_agent.shot_frames import advisory

    assert advisory("dialogue held in one cut: two people speak ...")
    assert not advisory(
        'the cut at 2 s falls in the middle of X\'s line "dialogue held in one cut"'
    )


def test_the_planner_is_told_the_coverage_length():
    from drama_agent.shot_frames import COVERAGE_MS, plan_instructions

    assert f"A clip of {COVERAGE_MS / 1000:g} s or more" in plan_instructions()


def test_the_writer_knows_which_pictures_show_a_real_person(package):
    take = face_take(package)
    take.references[0] = take.references[0].model_copy(update={"likeness": True})
    pictures = picture_sentences(package, take)
    assert pictures[0]["likeness"] is True and pictures[1]["likeness"] is False
    rules = writer_instructions()
    assert "Rule 3 applies only to a person whose picture has `likeness: true`" in rules
    assert "follow such a direction within rules 1 and 2" in rules


def test_the_judge_is_told_the_direction(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1, "direction": "his shirt off"})
        b.wait()
        check = b.calls("shot_check")[-1]
        assert check.context["direction"] == "his shirt off"
        assert "Direction: <what was asked>" in check.instructions


# ---------------------------------------------------------------------------
# Choosing the pictures by reading the scene (shot_reading.py)


def test_a_close_shot_is_drawn_from_the_picture_of_its_part_of_the_set(package, monkeypatch):
    """A standard set has a mid-shot of each of its areas. The scene's reading says which
    area is behind each cut; with DRAMA_KEYFRAME_SET_AREAS=1 that picture replaces the wide
    view, when it has been drawn. Off by default: the first trial drew worse frames."""
    from drama_agent import shot_reading
    from drama_agent.shot_reading import ReadingOutput, SceneReading, ViewOut
    from drama_agent.shots.picks import shot_picks

    shot = next(s for s in package.shot_list if s.location_id == "loc_shop")
    plan = single_plan(package, shot)
    sub = plan.sub(1)
    images = done_images(package)
    req = next(r for r in package.asset_requirements.items
               if r.asset_type == "location" and r.requirement_id in shot.asset_requirement_ids)  # fmt: skip
    for i in images:  # the helper's ids are cut short: give this set's pictures their own
        if i.requirement_id == req.requirement_id:
            i.image_id = "img_" + i.deliverable_id.removeprefix(f"d_{req.requirement_id}_")
    picks = shot_picks(package, images, shot)
    assert sorted(picks.areas) == [1, 2, 3, 4, 5, 6]
    assert "Front door, the part of it behind this framing" in picks.areas[3].role

    def set_ref(reading):
        refs, _ = sub_shot_references(package, images, [], shot, sub, reading=reading)
        return next(r for r in refs if r.kind == "set")

    assert set_ref(None).ref_id == "img_wide"
    reading = SceneReading(package_id=package.package_id, revision_id=package.revision_id,
                           scene_id=shot.scene_id, set_views={f"{shot.shot_id}:1": 3})  # fmt: skip
    assert set_ref(reading).ref_id == "img_wide"  # not unless switched on
    monkeypatch.setenv("DRAMA_KEYFRAME_SET_AREAS", "1")
    assert set_ref(reading).ref_id == "img_area_3"
    # The wide view for a wide shot (0), and for an area whose picture isn't drawn yet.
    reading.set_views = {f"{shot.shot_id}:1": 0}
    assert set_ref(reading).ref_id == "img_wide"
    reading.set_views = {f"{shot.shot_id}:1": 3}
    images = [i for i in images if i.image_id != "img_area_3"]
    assert set_ref(reading).ref_id == "img_wide"

    # The reading is told the areas and its choices are checked; a set that lists no
    # areas reads, and is fingerprinted, as before.
    ctx = shot_reading.reading_context(package, shot.scene_id, {}, [])
    assert [a["name"] for a in ctx["scene"]["areas"]][:2] == [
        "Behind the counter", "Customer side of the counter"]  # fmt: skip
    out = ReadingOutput(views=[ViewOut(shot_id=shot.shot_id, sub_shot=1, area=2),
                               ViewOut(shot_id=shot.shot_id, sub_shot=9, area=1),
                               ViewOut(shot_id="sh_other", sub_shot=1, area=1)])  # fmt: skip
    assert shot_reading.read_views(package, shot.scene_id, {}, out) == {f"{shot.shot_id}:1": 2}
    out.views[0].area = 7
    assert shot_reading.read_views(package, shot.scene_id, {}, out) == {}
    with_areas = shot_reading.fingerprint(package, shot.scene_id, {}, images)
    package.set_sheet.locations[0].areas = []
    assert "areas" not in shot_reading.reading_context(package, shot.scene_id, {}, [])["scene"]
    assert shot_reading.fingerprint(package, shot.scene_id, {}, images) != with_areas


def test_reading_the_scene_runs_on_gpt_6_luna_unless_set(monkeypatch):
    from drama_agent import config

    for k in list(__import__("os").environ):
        if k.startswith("MCPHUB_MODEL_"):
            monkeypatch.delenv(k)
    for k, v in {"DRAMA_MODEL_CLIENT": "mcphub", "MCPHUB_URL": "http://x", "MCPHUB_KEY": "k",
                 "MCPHUB_MODEL": "gpt-6.1-sol"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    client = config.client_from_env()
    assert client.model_for("shot_reading") == "gpt-6-luna"
    assert client.model_for("shot_frames") == "gpt-6.1-sol"
    monkeypatch.setenv("MCPHUB_MODEL_SHOT_READING", "gpt-6.1-sol")
    assert config.client_from_env().model_for("shot_reading") == "gpt-6.1-sol"


class FaceForTheFirst(FixtureModelClient):
    """The reading draws the first person of every sub-shot from their face picture, in a
    state of its own (hair loose, nothing in hand)."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_reading":
            pics = request.context["pictures"]
            for st in out.states:
                face = next((p["image"] for p in pics
                             if p["person"] == st.person and p["meant_as"].startswith("face")), None)  # fmt: skip
                if face and st.person == request.context["shots"][0]["sub_shots"][0]["people"][0]:
                    st.image, st.hair, st.carrying, st.keep = face, "loose to the shoulders", "", []
                    st.reason = "Closest for the moment."
        return out


def test_a_written_take_is_drawn_from_the_pictures_the_reading_chose(tmp_path):
    with Board(tmp_path, text=FaceForTheFirst()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        reading = b.calls("shot_reading")[-1]
        assert reading.media and reading.media[0].label == "<Image 1>"
        first = b.shot(sid)["sub_shots"][0]["choices"][0]
        assert first["state"]["hair"] == "loose to the shoulders" and first["source"] == "model"
        ref = next(r for r in take["references"] if r["entity_id"] == first["character_id"])
        assert ref["image_id" if "image_id" in ref else "ref_id"] == first["image_id"]
        assert ref["state"]["hair"] == "loose to the shoulders"
        # The prompt says how they look now, not "every garment and accessory".
        assert "hair loose to the shoulders, with empty hands" in take["prompt"]
        assert "every garment and accessory" not in take["prompt"]
        # The judge checks the wardrobe against that state.
        check = b.calls("shot_check")[-1]
        people = check.context["sub_shot"]["people"]
        assert people[0]["now"]["hair"] == "loose to the shoulders"
        assert b.shot(sid)["reading"]["fresh"] is True


def test_a_person_can_choose_another_picture_and_it_survives_a_new_reading(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        sub = b.shot(sid)["sub_shots"][0]
        c = sub["choices"][0]
        other = next(x["image_id"] for x in c["candidates"] if x["image_id"] != c["image_id"])
        r = b.http.post(f"{b.base}/{sid}/choice", json={"sub_shot": 1, "character_id":
                        c["character_id"], "image_id": other})  # fmt: skip
        assert r.status_code == 200, r.text
        # Someone else's picture is refused.
        foreign = next(x["image_id"] for ch in sub["choices"][1:] for x in ch["candidates"])
        r = b.http.post(f"{b.base}/{sid}/choice", json={"sub_shot": 1, "character_id":
                        c["character_id"], "image_id": foreign})  # fmt: skip
        assert r.status_code == 422
        scene = b.shot(sid)["scene_id"]
        b.http.post(f"{b.base}/scenes/{scene}/reading", json={})
        b.wait()
        assert len(b.calls("shot_reading")) == 2
        mine = b.shot(sid)["sub_shots"][0]["choices"][0]
        assert (mine["image_id"], mine["source"]) == (other, "user")
        # The next take draws them from it.
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        assert any(x["ref_id"] == other for x in r.json()["references"])


def test_a_new_plan_makes_the_reading_stale_and_it_is_read_again(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        assert len(b.calls("shot_reading")) == 1 and b.shot(sid)["reading"]["fresh"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        assert b.shot(sid)["reading"]["fresh"] is False
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        assert len(b.calls("shot_reading")) == 2 and b.shot(sid)["reading"]["fresh"]
        assert [len(s["choices"]) for s in b.shot(sid)["sub_shots"]] == [len(shot["people"]), 1]


class WrongPicture(FixtureModelClient):
    """The reading gives a person someone else's picture, every time."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_reading":
            pics = request.context["pictures"]
            for st in out.states:
                st.image = next(p["image"] for p in pics if p["person"] != st.person)
        return out


def test_a_reading_that_cant_be_used_leaves_the_storyboard_looks(tmp_path):
    with Board(tmp_path, text=WrongPicture()) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        assert len(b.calls("shot_reading")) == 2  # tried once more with what was wrong
        assert b.calls("shot_reading")[-1].context["problems"]
        assert take["status"] == "done"
        assert all(r.get("state") is None for r in take["references"])
        s = b.shot(sid)
        assert s["reading"]["status"] == "failed" and "is not a picture of" in s["reading"]["error"]


def test_the_previous_shots_keyframe_opens_the_next_shot(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shots = b.board()["shots"]
        a, nxt = shots[0], shots[1]
        assert a["scene_id"] == nxt["scene_id"]
        b.http.post(f"{b.base}/{a['shot_id']}", json={"draws": 1})
        [take] = b.wait() and b.takes(a["shot_id"])
        b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0})
        r = b.http.post(f"{b.base}/{nxt['shot_id']}", json={"draws": 1})
        refs = r.json()["references"]
        kinds = [x["kind"] for x in refs]
        before = refs[kinds.index("previous_shot")]
        assert (
            before["ref_id"] == f"frame:{take['take_id']}:0" and "previous shot" in before["role"]
        )
        # After the people and the set: it never pushes out a look picture.
        assert kinds.index("previous_shot") > kinds.index("set")
        assert all(k in ("look", "face", "set") for k in kinds[: kinds.index("previous_shot")])
        # It lends the set and light, not the people or their clothes.
        b.wait()
        prompt = b.takes(nxt["shot_id"])[0]["prompt"]
        n = kinds.index("previous_shot") + 1
        assert (f"<Subject {n}> serves as the reference for the set, the light and the props"
                in prompt)  # fmt: skip
        # The board shows the same pictures as the draw.
        board_refs = b.shot(nxt["shot_id"])["sub_shots"][0]["references"]
        assert [x["ref_id"] for x in board_refs] == [x["ref_id"] for x in refs]


def test_a_version_is_drawn_with_the_pictures_it_was_written_for(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        [v1] = b.shot(sid)["sub_shots"][0]["versions"]
        assert [r["ref_id"] for r in v1["references"]] == [r["ref_id"] for r in take["references"]]
        c = b.shot(sid)["sub_shots"][0]["choices"][0]
        other = next(x["image_id"] for x in c["candidates"] if x["image_id"] != c["image_id"])
        b.http.post(f"{b.base}/{sid}/choice", json={"sub_shot": 1, "character_id":
                    c["character_id"], "image_id": other})  # fmt: skip
        r = b.http.post(f"{b.base}/{sid}", json={"prompt_version": 1, "draws": 1})
        assert [x["ref_id"] for x in r.json()["references"]] == [
            x["ref_id"] for x in v1["references"]
        ]


def test_the_export_says_how_each_person_looks_in_each_cut(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        spec = json.loads(member(b.zip(), "shot_keyframes/sub_shots.json"))
        mine = next(p for p in spec["shots"] if p["shot_id"] == sid)
        now = mine["sub_shots"][0]["now"]
        assert now and all(v["picture"].startswith("reference_images/") for v in now.values())
        assert all("wearing" in v for v in now.values())


def test_a_version_opens_from_the_keyframe_approved_now(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 2})
        [one] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
        b.wait()
        [v1] = b.shot(sid)["sub_shots"][1]["versions"]
        assert v1["references"][0]["ref_id"] == f"frame:{one['take_id']}:0"
        # Another sub-shot 1 draw is approved: v1 is drawn from it, same slot.
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 1, "next": False})
        r = b.http.post(f"{b.base}/{sid}", json={"sub_shot": 2, "prompt_version": 1})
        assert r.status_code == 202, r.text
        assert r.json()["references"][0]["ref_id"] == f"frame:{one['take_id']}:1"
        b.wait()
        # Withdrawn: drawing v1 is refused with the reason.
        b.http.delete(f"/v1/shot-takes/{one['take_id']}/approve")
        r = b.http.post(f"{b.base}/{sid}", json={"sub_shot": 2, "prompt_version": 1})
        assert r.status_code == 409


def test_your_own_version_keeps_its_pictures(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        mine = take["prompt"].replace("No other lettering", "Soft rain. No other lettering")
        v = b.http.post(f"{b.base}/{sid}/prompts", json={"prompt": mine}).json()
        assert [r["ref_id"] for r in v["references"]] == [r["ref_id"] for r in take["references"]]


class ReadingFailsLater(FixtureModelClient):
    """The first reading works; every later one fails."""

    readings = 0

    async def generate(self, request, output_model):
        if request.stage == "shot_reading":
            self.readings += 1
            if self.readings > 1:
                await asyncio.sleep(0.5)  # long enough for a second click to arrive
                raise RuntimeError("model unavailable")
        return await super().generate(request, output_model)


def test_a_failed_re_read_keeps_the_good_reading_and_your_choices(tmp_path):
    with Board(tmp_path, text=ReadingFailsLater()) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        c = b.shot(sid)["sub_shots"][0]["choices"][0]
        other = next(x["image_id"] for x in c["candidates"] if x["image_id"] != c["image_id"])
        b.http.post(f"{b.base}/{sid}/choice", json={"sub_shot": 1, "character_id":
                    c["character_id"], "image_id": other})  # fmt: skip
        scene = b.shot(sid)["scene_id"]
        assert b.http.post(f"{b.base}/scenes/{scene}/reading", json={}).status_code == 202
        # Being read: a second click and a choice change are refused.
        assert b.http.post(f"{b.base}/scenes/{scene}/reading", json={}).status_code == 409
        b.wait()
        s = b.shot(sid)
        assert s["reading"]["fresh"] and "model unavailable" in s["reading"]["error"]
        assert s["sub_shots"][0]["choices"][0]["image_id"] == other


def test_a_choice_cant_change_while_the_scene_is_read(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        c = b.shot(sid)["sub_shots"][0]["choices"][0]
        pkg = b.repo.get_package(b.pid)
        reading = b.repo.get_scene_reading(pkg.package_id, pkg.revision_id, b.shot(sid)["scene_id"])
        b.repo.save_scene_reading(reading.model_copy(update={"status": "writing"}))
        r = b.http.post(f"{b.base}/{sid}/choice", json={"sub_shot": 1, "character_id":
                        c["character_id"], "image_id": c["image_id"]})  # fmt: skip
        assert r.status_code == 409 and "being read" in r.json()["detail"]


def test_a_scene_with_no_picks_isnt_read(tmp_path):
    with Board(tmp_path) as b:
        scene = b.board()["shots"][0]["scene_id"]
        r = b.http.post(f"{b.base}/scenes/{scene}/reading", json={})
        assert r.status_code == 409 and "pick design pictures" in r.json()["detail"]


def test_a_chosen_picture_unblocks_a_person_without_a_storyboard_pick(package):
    images = done_images(package)
    shot = busiest(package)
    who = shot.character_ids[0]
    look_req = next(r.requirement_id for r in package.asset_requirements.items
                    if r.asset_type == "character_look" and r.requirement_id in shot.asset_requirement_ids
                    and r.subject_refs[0].entity_id == who)  # fmt: skip
    without = [i for i in images if i.requirement_id != look_req]
    plan = single_plan(package, shot)
    with pytest.raises(NotReady, match="first: "):
        plan_take(package, without, [], plan)
    from drama_agent.shot_reading import Candidate, Choice, SceneReading

    face = next(i for i in without if i.requirement_id.startswith("ar_character_identity")
                and who in i.requirement_id)  # fmt: skip
    reading = SceneReading(
        package_id=package.package_id, revision_id=package.revision_id, scene_id=shot.scene_id,
        candidates=[Candidate(image_id=face.image_id, character_id=who, kind="face", label="face")],
        choices=[Choice(shot_id=shot.shot_id, sub_shot=1, character_id=who,
                        state={"wearing": "a robe"}, image_id=face.image_id)],
    )  # fmt: skip
    take = plan_take(package, without, [], plan, reading=reading)
    mine = next(r for r in take.references if r.entity_id == who)
    assert mine.ref_id == face.image_id and mine.state.wearing == "a robe"


def test_a_reading_for_a_cut_or_person_that_isnt_there_is_refused(package):
    from drama_agent.shot_reading import (
        PictureOut,
        ReadingOutput,
        StateOut,
        candidates,
        read_output,
    )

    shot = busiest(package)
    images = done_images(package)
    who = shot.character_ids[0]
    names = {c.character_id: c.name for c in package.story_bible.characters}
    pool = candidates(package, images, who)
    out = ReadingOutput(pictures=[PictureOut(image="<Image 1>")], states=[
        StateOut(shot_id=shot.shot_id, sub_shot=3, person=names[who], image="<Image 1>"),
    ])  # fmt: skip
    with pytest.raises(ValueError, match="has no sub-shot 3"):
        read_output(package, shot.scene_id, {}, pool, out)


def _specs(package, plans=(), keyframes=None, images=None, readings=None):
    from drama_agent.export import Names, fresh_readings
    from drama_agent.shot_specs import shot_specs

    images = images if images is not None else done_images(package)
    stored = {p.shot_id: p for p in plans}
    exported = {i.image_id: f"{i.requirement_id}.jpg" for i in images}
    fresh = fresh_readings(package, stored, readings or [], images)
    return shot_specs(package, stored, keyframes or {}, Names(package), fresh, images, exported)


def test_shot_specs_are_complete_for_video_production(package):
    shot = busiest(package)
    plan = two_cuts(package, shot)
    frames = {(shot.shot_id, 1): "a_s1.jpg", (shot.shot_id, 2): "a_s2.jpg"}
    out = _specs(package, [plan], frames)
    assert out["spec_version"] == 3 and out["max_references"] == 9
    assert [s["shot_id"] for s in out["shots"]] == [s.shot_id for s in package.shot_list]
    mine = next(s for s in out["shots"] if s["shot_id"] == shot.shot_id)
    # Every cut has its keyframe; only the video prompt is missing (test_video_prompt.py).
    assert mine["not_ready"] == ["it has no video prompt yet"] and mine["keyframe_first"]
    assert mine["prompt"] is None and all(r["named"] is None for r in mine["references"])
    spec = mine["spec"]
    assert spec["action"] == shot.visual_action and spec["camera"]["shot_size"] == shot.framing
    assert spec["target_duration_ms"] == plan.sub_shots[-1].end_ms
    assert [s["keyframe"] for s in spec["sub_shots"]] == [
        "05_assets/shot_keyframes/a_s1.jpg", "05_assets/shot_keyframes/a_s2.jpg"]  # fmt: skip
    # Keyframe-first: the keyframes, then the faces, then everything else.
    kinds = [r["kind"] for r in mine["references"]]
    assert kinds[:2] == ["keyframe", "keyframe"] and kinds[2] == "face"
    assert [r["sub_shots"] for r in mine["references"][:2]] == [[1], [2]]
    assert kinds.index("set") == len(kinds) - 1
    assert all(r["file"].startswith("05_assets/") for r in mine["references"])
    assert len({r["file"] for r in mine["references"]}) == len(kinds)
    people = [c for c in mine["cast"] if c["kind"] == "character"]
    assert {c["entity_id"] for c in people} == set(shot.character_ids)
    assert any(c["kind"] == "location" for c in mine["cast"])


def test_a_shot_without_its_keyframes_is_not_ready(package):
    shot = busiest(package)
    out = _specs(package, [two_cuts(package, shot)], {(shot.shot_id, 1): "a_s1.jpg"})
    mine = next(s for s in out["shots"] if s["shot_id"] == shot.shot_id)
    assert not mine["ready"] and not mine["keyframe_first"]
    assert mine["not_ready"] == ["[Shot 2] has no approved keyframe", "it has no video prompt yet"]
    # The keyframe it has, the faces, then each person's picture, the set and the props.
    kinds = [r["kind"] for r in mine["references"]]
    assert kinds[0] == "keyframe" and kinds[1] == "face"
    faces = [i for i, k in enumerate(kinds) if k == "face"]
    assert faces == list(range(1, len(faces) + 1)) and kinds.index("set") > faces[-1]


def test_an_unplanned_shot_keeps_the_storyboard_length(package):
    out = _specs(package)
    shot = package.shot_list[0]
    mine = out["shots"][0]
    assert mine["spec"]["sub_shots"] is None
    assert mine["spec"]["target_duration_ms"] == shot.duration_ms
    assert not mine["ready"] and mine["planned_by"] == "single"
    assert not any(r["kind"] == "keyframe" for r in mine["references"])


def test_a_line_over_a_cut_is_spoken_once(package):
    shot_ids = [s.shot_id for s in package.shot_list]
    first = next(s for s in package.shot_list if s.dialogue_cues)
    nxt = next(s for s in package.shot_list if s.episode_id == first.episode_id
               and s.start_ms > first.start_ms)  # fmt: skip
    nxt.dialogue_cues = [first.dialogue_cues[-1], *nxt.dialogue_cues]
    out = {s["shot_id"]: s for s in _specs(package)["shots"]}
    assert len(out) == len(shot_ids)
    cue = first.dialogue_cues[-1].dialogue_id
    assert any(d["id"] == cue for d in out[first.shot_id]["spec"]["dialogue"])
    assert all(d["id"] != cue for d in out[nxt.shot_id]["spec"]["dialogue"])
    assert "over the cut" in out[nxt.shot_id]["spec"]["continuity_notes"]
    assert [c["id"] for c in out[nxt.shot_id]["spec"]["carried"]] == [cue]


def test_shot_specs_use_the_readings_pictures_and_notes(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = first_shot(b)
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0})
        z = b.zip()
        specs = json.loads(member(z, "03_storyboard/shot_specs.json"))
        mine = next(s for s in specs["shots"] if s["shot_id"] == sid)
        names = set(z.namelist())
        root = next(iter(names)).split("/")[0]
        # Every file it names is in the export.
        assert all(f"{root}/{r['file']}" in names for r in mine["references"])
        assert mine["references"][0]["kind"] == "keyframe"
        assert any(r["kind"] == "chosen" for r in mine["references"])
        notes = [c["note"] for c in mine["cast"] if c["kind"] == "character"]
        assert notes and all(n.startswith("Wearing:") or not n for n in notes)
        assert mine["spec"]["sub_shots"][0]["now"]


def test_keyframes_are_drawn_at_about_four_megapixels(package):
    from drama_agent.shot_frames import frame_size

    assert frame_size(package) == "1536x2720"
    take = plan_take(package, done_images(package), [], single_plan(package, busiest(package)))
    assert take.size == "1536x2720" and take.provider == "h3"
    w, h = (int(x) for x in take.size.split("x"))
    assert w % 32 == 0 and h % 32 == 0


def test_keyframe_short_edge_draws_smaller_keyframes(package, monkeypatch):
    from drama_agent.shot_frames import frame_size
    from drama_agent.shots.common import (
        KEYFRAME_SIZES,
        keyframe_aspect,
        keyframe_short_edge,
        scaled_keyframe_size,
    )

    monkeypatch.setenv("DRAMA_KEYFRAME_SHORT_EDGE", "1024")
    assert frame_size(package) == "1024x1824"
    take = plan_take(package, done_images(package), [], single_plan(package, busiest(package)))
    assert take.size == "1024x1824"
    # Both a small keyframe and one drawn at the full size before still name their aspect.
    assert keyframe_aspect("1024x1824") == keyframe_aspect("1536x2720") == "9:16"
    assert keyframe_aspect("1000x1000") is None
    for full in KEYFRAME_SIZES.values():
        w, h = (int(x) for x in scaled_keyframe_size(full).split("x"))
        assert w % 32 == 0 and h % 32 == 0 and w * h < 2_100_000
    monkeypatch.setenv("DRAMA_KEYFRAME_SHORT_EDGE", "768")
    assert frame_size(package) == "768x1344"
    # Never larger than the table, never tiny, and a bad value is ignored.
    for value, edge in (("4096", 1536), ("8", 256), ("big", 1536), ("", 1536)):
        monkeypatch.setenv("DRAMA_KEYFRAME_SHORT_EDGE", value)
        assert keyframe_short_edge() == edge
    assert frame_size(package) == "1536x2720"


def test_a_take_can_be_drawn_with_nano_banana(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        board = b.board()
        assert board["providers"] == {
            "h3": "fixture-image-v1",
            "nano-banana": "fixture-nano-banana",
        }
        assert board["default_provider"] == "h3"
        sid = first_shot(b)
        before = len(b.images.calls)  # the design pictures
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 1, "provider": "nano-banana"})
        assert r.status_code == 202, r.text
        assert r.json()["provider"] == "nano-banana" and len(r.json()["references"]) <= 8
        [take] = b.wait() and b.takes(sid)
        assert take["results"][0]["model"] == "fixture-nano-banana"
        assert b.banana.calls and len(b.images.calls) == before
        # A provider the server doesn't have is refused, naming the ones it has.
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 1, "provider": "dall-e"})
        assert r.status_code == 422 and "nano-banana" in r.json()["detail"]


def test_nano_banana_takes_eight_pictures(package):
    shot = busiest(package)
    many = plan_take(package, done_images(package), [], single_plan(package, shot))
    eight = plan_take(package, done_images(package), [], single_plan(package, shot),
                      provider="nano-banana", max_references=2)  # fmt: skip
    assert len(eight.references) == 2 and eight.references == many.references[:2]


def test_the_gemini_request_and_its_answers():
    import base64

    import httpx

    from drama_agent.imagegen import (
        GeminiImageClient,
        GeminiImageError,
        Reference,
        gemini_aspect,
        gemini_image,
        gemini_size,
    )

    assert gemini_aspect("1536x2720") == "9:16" and gemini_size("1536x2720") == "2K"
    assert gemini_aspect("2048x2048") == "1:1" and gemini_size("1024x1024") == "1K"
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["key"] = request.headers["x-goog-api-key"]
        sent["body"] = json.loads(request.content)
        png = base64.b64encode(b"\x89PNG fake").decode()
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": png}}]}}]})  # fmt: skip

    client = GeminiImageClient("k", transport=httpx.MockTransport(handler))
    refs = [Reference("a.jpg", b"aaa"), Reference("b.png", b"bbb", "image/png")]
    out = asyncio.run(client.generate("<Picture 2> is <Subject 1>. <picture 1> stays.",
                                      "1536x2720", 7, refs))  # fmt: skip
    assert out.content_type == "image/png" and out.data == b"\x89PNG fake"
    assert sent["url"].endswith("/models/gemini-nano-banana-2.1:generateContent")
    assert sent["key"] == "k"
    parts = sent["body"]["contents"][0]["parts"]
    assert [p.get("text") for p in parts[::2][:2]] == ["Picture 1:", "Picture 2:"]
    assert parts[3]["inlineData"]["mimeType"] == "image/png"
    assert parts[-1]["text"] == "Picture 2 is <Subject 1>. Picture 1 stays."
    config = sent["body"]["generationConfig"]
    assert config["imageConfig"] == {"aspectRatio": "9:16", "imageSize": "2K"}
    assert config["seed"] == 7 and config["responseModalities"] == ["IMAGE"]
    with pytest.raises(Exception, match="at most 8"):
        asyncio.run(
            client.generate("x", "1536x2720", 1, [Reference(str(i), b"x") for i in range(9)])
        )
    # A refusal says so; an empty answer gives the model's reason.
    with pytest.raises(GeminiImageError) as e:
        gemini_image({"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]})
    assert e.value.refused and "IMAGE_SAFETY" in str(e.value)
    with pytest.raises(GeminiImageError) as e:
        gemini_image({"candidates": [{"finishReason": "STOP",
                                      "content": {"parts": [{"text": "I can't"}]}}]})  # fmt: skip
    assert not e.value.refused and "I can't" in str(e.value)
    with pytest.raises(GeminiImageError, match="blocked"):
        gemini_image({"promptFeedback": {"blockReason": "OTHER"}})


def test_a_missing_script_line_keeps_the_shot_from_ready(package):
    shot = busiest(package)
    from drama_agent.models.storyboard import DialogueCue

    cue = shot.dialogue_cues[0] if shot.dialogue_cues else None
    shot.dialogue_cues = [*shot.dialogue_cues, DialogueCue(
        dialogue_id="dl_gone", start_ms=shot.start_ms, end_ms=shot.start_ms + 500)]  # fmt: skip
    frames = {(shot.shot_id, 1): "a_s1.jpg"}
    out = _specs(package, [single_plan(package, shot)], frames)
    mine = next(s for s in out["shots"] if s["shot_id"] == shot.shot_id)
    assert not mine["ready"] and "dialogue dl_gone is not in the script" in mine["not_ready"]
    assert cue is None or any(d["id"] == cue.dialogue_id for d in mine["spec"]["dialogue"])


class NewHairEachRead(FixtureModelClient):
    """Every reading after the first gives everyone a different hairstyle: a model that
    doesn't repeat itself word for word."""

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if request.stage == "shot_reading" and request.attempt == 0:
            n = len([r for r in self.calls if r.stage == "shot_reading"])
            if n > 1:
                for st in out.states:
                    st.hair = f"pinned up (reading {n})"
        return out


def test_a_new_reading_keeps_the_choices_of_the_cuts_that_didnt_change(tmp_path):
    with Board(tmp_path, text=NewHairEachRead()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        others = [s for s in b.board()["shots"] if s["scene_id"] == shot["scene_id"]
                  and s["shot_id"] != sid]  # fmt: skip
        assert others, "the sample scene has more than one shot"
        oid = others[0]["shot_id"]
        b.http.post(f"{b.base}/{oid}", json={"draws": 1})
        b.wait()
        before = b.shot(oid)["sub_shots"][0]["choices"]
        assert before and all(c["state"]["hair"] != "pinned up (reading 2)" for c in before)
        # Another shot of the scene is re-planned: the reading is stale and read again.
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        assert len(b.calls("shot_reading")) == 2
        # The re-planned shot is read anew; the untouched shot keeps its choices.
        assert all(c["state"]["hair"] == "pinned up (reading 2)"
                   for s in b.shot(sid)["sub_shots"] for c in s["choices"])  # fmt: skip
        assert b.shot(oid)["sub_shots"][0]["choices"] == before


def test_keyframe_references_are_shrunk_to_the_keyframes_size(tmp_path, package, monkeypatch):
    """A design picture is 16 MP; sent as it is, a few of them crash MCPHub's model
    server. A reference goes up at the keyframe's short edge at most."""
    import subprocess

    from drama_agent.shots import assemble as asm
    from drama_agent.shots.common import keyframe_short_edge
    from drama_agent.shots.models import FrameRef, ShotTake
    from drama_agent.shots.runner import ShotFrameRunner
    from drama_agent.videogen import ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("ffmpeg is not installed")
    repo = InMemoryRepository()
    repo.save_package(package, True)
    media = MediaStore(tmp_path)
    big = media.path("generated/big.jpg")
    big.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=3552x4736",
                    "-frames:v", "1", str(big)], check=True)  # fmt: skip
    [img] = done_images(package)[:1]
    img.file = "generated/big.jpg"
    repo.save_image(img)
    shot = package.shot_list[0]
    take = ShotTake(take_id="stk_test", run_id="stk_test", package_id=package.package_id,
                    revision_id=package.revision_id, shot_id=shot.shot_id, sub_shot=1,
                    prompt="p", size="1536x2720", references=[
                        FrameRef(ref_id=img.image_id, role="a look", kind="look")])  # fmt: skip
    runner = ShotFrameRunner(repo, media, FixtureImageClient(), FixtureModelClient())
    [ref] = runner._reference_bytes(take)
    sent = tmp_path / "sent.jpg"
    sent.write_bytes(ref.data)
    info = asm.probe(sent)
    assert min(info["width"], info["height"]) == keyframe_short_edge() == 1536
    assert ref.mime == "image/jpeg" and len(ref.data) < big.stat().st_size
    # A smaller keyframe sends its references smaller with it.
    monkeypatch.setenv("DRAMA_KEYFRAME_SHORT_EDGE", "768")
    [ref] = runner._reference_bytes(take)
    sent.write_bytes(ref.data)
    info = asm.probe(sent)
    assert min(info["width"], info["height"]) == 768
