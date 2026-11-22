"""The history of a shot's cut plans (shots/history.py): kept when replaced, restored with
the keyframes approved for it."""

import pytest

from drama_agent.models.package import utcnow
from drama_agent.shot_frames import ShotPlan, ShotPlanVersion, SubShot
from drama_agent.shots.staleness import KF_BEFORE_MISSING
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

from .test_shot_frames import BadPlanner, Board, TwoCuts


def _approved(b, sid) -> list:
    return [s["approved"] for s in b.shot(sid)["sub_shots"]]


def _draw_and_approve_both(b, sid) -> None:
    """Draw the first cut, approve it (which starts the second), approve the second."""
    b.http.post(f"{b.base}/{sid}", json={"draws": 1})
    [one] = b.wait() and b.takes(sid)
    b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
    [two] = b.wait() and b.takes(sid, 2)
    b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0, "next": False})


def test_a_re_plan_keeps_the_old_plan_and_a_restore_brings_its_keyframes_back(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        mine = b.two_cuts(shot)
        b.http.post(f"{b.base}/{sid}/plan", json=mine)
        assert b.shot(sid)["plan"]["history"] == 0  # nothing was replaced
        _draw_and_approve_both(b, sid)
        assert None not in _approved(b, sid)

        # The model re-plans it: the old plan is kept, with both approvals.
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert s["plan"]["source"] == "model" and s["plan"]["history"] == 1
        assert _approved(b, sid) == [None, None]  # the new cuts have no keyframes yet
        [v] = b.http.get(f"{b.base}/{sid}/plans").json()
        assert v["version"] == 1 and v["replaced_by"] == "model"
        assert v["plan"]["source"] == "user" and len(v["approved"]) == 2
        assert [x["framing"] for x in v["plan"]["sub_shots"]] == ["wide", "close-up"]

        # Restoring it brings its keyframes back, approved; the model's plan takes its place
        # in the history (so swapping back and forth never piles up copies).
        r = b.http.post(f"{b.base}/{sid}/plans/1/restore", json={})
        assert r.status_code == 200, r.text
        assert r.json()["keyframes_approved"] == 2
        s = b.shot(sid)
        assert s["plan"]["source"] == "user" and s["plan"]["history"] == 1
        assert [x["framing"] for x in s["sub_shots"]] == ["wide", "close-up"]
        assert None not in _approved(b, sid)
        newest = b.http.get(f"{b.base}/{sid}/plans").json()[0]
        assert newest["version"] == 2 and newest["replaced_by"] == "restore"
        assert newest["plan"]["source"] == "model" and newest["plan"]["notes"] == "two setups"

        # A version the shot doesn't have.
        assert b.http.post(f"{b.base}/{sid}/plans/9/restore", json={}).status_code == 404


def test_saving_the_same_cuts_or_a_failed_plan_adds_nothing(tmp_path):
    with Board(tmp_path, text=BadPlanner()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        mine = b.two_cuts(shot)
        b.http.post(f"{b.base}/{sid}/plan", json=mine)
        b.http.post(f"{b.base}/{sid}/plan", json=mine)  # the same cuts again
        assert b.shot(sid)["plan"]["history"] == 0
        b.text.good = 0  # the next model plan breaks the rules: it fails
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert s["plan"]["status"] == "failed" and s["plan"]["history"] == 0
        assert s["plan"]["source"] == "user" and len(s["sub_shots"]) == 2


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_versions_are_numbered_by_the_store_and_move_with_a_revision(tmp_path, kind):
    repo = SqliteRepository(tmp_path / "d.db") if kind == "sqlite" else InMemoryRepository()
    sub = SubShot(index=1, start_ms=0, end_ms=4000, framing="wide", action="x")
    plan = ShotPlan(package_id="p", revision_id="r1", shot_id="s", source="user",
                    sub_shots=[sub])  # fmt: skip

    def add():
        return repo.add_plan_version(ShotPlanVersion(
            package_id="p", revision_id="r1", shot_id="s", version=0, plan=plan,
            approved={"t": 0}, replaced_at=utcnow()))  # fmt: skip

    assert [add().version, add().version] == [1, 2]
    assert repo.count_plan_versions("p", "r1") == {"s": 2}
    repo.delete_plan_version("p", "r1", "s", 1)
    assert [v.version for v in repo.list_plan_versions("p", "r1", "s")] == [2]
    assert add().version == 3
    assert repo.carry_forward("p", "r1", "r2") >= 2
    assert repo.list_plan_versions("p", "r1") == []
    vs = repo.list_plan_versions("p", "r2", "s")
    assert {v.revision_id for v in vs} == {"r2"} and {v.plan.revision_id for v in vs} == {"r2"}
    assert vs[0].approved == {"t": 0} and vs[0].plan.sub_shots == [sub]


def test_a_re_plan_with_another_clip_keeps_the_old_plans_own_clip(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={**b.two_cuts(shot), "clip_s": 15})
        b.http.post(f"{b.base}/{sid}/plan", json={"clip_s": 12})
        s = b.wait() and b.shot(sid)
        assert s["clip_ms"] == 12000
        [v] = b.http.get(f"{b.base}/{sid}/plans").json()
        assert v["plan"]["clip_ms"] == 15000
        assert v["plan"]["sub_shots"][-1]["end_ms"] == 15000


def test_a_restore_counts_the_approvals_whose_draw_still_exists(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        _draw_and_approve_both(b, sid)
        first = b.takes(sid)[0]["take_id"]
        second = b.takes(sid, 2)[0]["take_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={})
        b.wait()
        # The first cut's take is gone: the second comes back approved on its own, marked
        # out of date (the cut before it has no keyframe).
        b.repo.delete_shot_take(first)
        r = b.http.post(f"{b.base}/{sid}/plans/1/restore", json={})
        assert r.status_code == 200 and r.json()["keyframes_approved"] == 1
        assert _approved(b, sid) == [None, [second, 0]]
        assert b.shot(sid)["sub_shots"][1]["outdated"] == [KF_BEFORE_MISSING]


def test_a_plan_that_no_longer_fits_the_shot_is_not_restored(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}/plan", json={})
        b.wait()
        # As if a revision took someone out of the shot after this plan was kept.
        [v] = b.repo.plan_versions
        v.plan.sub_shots[0].people.append("ch_nobody")
        r = b.http.post(f"{b.base}/{sid}/plans/1/restore", json={})
        assert r.status_code == 422 and "no longer fits" in r.json()["detail"]
        assert b.shot(sid)["plan"]["source"] == "model"
