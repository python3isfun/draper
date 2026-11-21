"""Inserting a cut between two keyframes: the model writes it, the other cuts keep their view
and their approved keyframes (moved to their new numbers), and the cut after the new one is
out of date until it is redrawn from it."""

import pytest

from drama_agent.llm.base import GenerationRequest
from drama_agent.shots.inserts import kept_cuts, new_index
from drama_agent.shots.models import MAX_SUB_SHOTS, PLAN_STAGE, SubShot
from drama_agent.shots.planner import fixture_plan
from drama_agent.shots.staleness import KF_CUT_INSERTED

from .test_shot_frames import Board


def _approved_two_cuts(b):
    """The busiest shot cut in two by hand, both keyframes drawn and approved."""
    b.design()
    shot = b.busiest()
    sid = shot["shot_id"]
    assert b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot)).status_code == 202
    b.http.post(f"{b.base}/{sid}", json={"draws": 1})
    [one] = b.wait() and b.takes(sid)
    b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})  # starts cut 2
    [two] = b.wait() and b.takes(sid, 2)
    b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
    return sid, one, two


def test_an_inserted_cut_keeps_the_other_keyframes(tmp_path):
    with Board(tmp_path) as b:
        sid, one, two = _approved_two_cuts(b)
        before = b.shot(sid)["sub_shots"]
        r = b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "her hand on the door"})
        assert r.status_code == 202 and r.json()["status"] == "writing"
        b.wait()
        call = b.calls("shot_plan")[-1]
        assert call.context["insert_note"] == "her hand on the door"
        assert call.context["insert_after_cut"] == 1 and len(call.context["current_plan"]) == 2
        assert "Inserting a cut" in call.instructions and "exactly 3 sub-shots" in call.instructions
        subs = b.shot(sid)["sub_shots"]
        assert len(subs) == 3 and "her hand on the door" in subs[1]["framing"]
        view = ("framing", "camera", "people", "placement", "off_screen")
        assert [{k: s[k] for k in view} for s in (subs[0], subs[2])] == [
            {k: s[k] for k in view} for s in before
        ]
        # Both keyframes stay approved, at their new numbers; the one after the new cut is
        # out of date, since it continues from cut 1.
        assert subs[0]["approved"] == [one["take_id"], 0] and subs[0]["outdated"] == []
        assert subs[2]["approved"] == [two["take_id"], 0]
        assert KF_CUT_INSERTED in subs[2]["outdated"]
        # The new cut is drawn from cut 1's keyframe.
        [new] = subs[1]["takes"]
        assert new["references"][0]["ref_id"] == f"frame:{one['take_id']}:0"
        # Approving it keeps cut 3 approved (out of date) and starts no redraw of it.
        b.http.post(f"/v1/shot-takes/{new['take_id']}/approve", json={"n": 0})
        b.wait()
        subs = b.shot(sid)["sub_shots"]
        assert subs[2]["approved"] == [two["take_id"], 0] and len(subs[2]["takes"]) == 1
        assert KF_CUT_INSERTED in subs[2]["outdated"]
        # Redrawn from the new cut and approved, it is up to date.
        b.http.post(f"{b.base}/{sid}", json={"sub_shot": 3, "draws": 1})
        b.wait()
        redrawn = b.takes(sid, 3)[-1]
        assert redrawn["references"][0]["ref_id"] == f"frame:{new['take_id']}:0"
        b.http.post(f"/v1/shot-takes/{redrawn['take_id']}/approve", json={"n": 0})
        subs = b.shot(sid)["sub_shots"]
        assert subs[2]["approved"] == [redrawn["take_id"], 0] and subs[2]["outdated"] == []
        # The plan before the insert is in the history.
        history = b.http.get(f"{b.base}/{sid}/plans").json()
        assert len(history[0]["plan"]["sub_shots"]) == 2


def test_a_cut_after_the_last_one_and_the_limits(tmp_path):
    with Board(tmp_path) as b:
        sid, _, two = _approved_two_cuts(b)
        assert (
            b.http.post(f"{b.base}/{sid}/insert", json={"after": 3, "note": "x"}).status_code == 422
        )
        r = b.http.post(
            f"{b.base}/{sid}/insert", json={"after": 2, "note": "the door closes", "draw": False}
        )
        assert r.status_code == 202
        b.wait()
        subs = b.shot(sid)["sub_shots"]
        assert len(subs) == 3 and subs[1]["approved"] == [two["take_id"], 0]
        assert subs[1]["outdated"] == [] and subs[2]["takes"] == []  # not drawn: draw was off
        for k in (1, 2):  # up to MAX_SUB_SHOTS cuts, each into the long first cut
            b.http.post(
                f"{b.base}/{sid}/insert", json={"after": 1, "note": f"beat {k}", "draw": False}
            )
            b.wait()
            assert b.shot(sid)["plan"]["error"] is None
        assert len(b.shot(sid)["sub_shots"]) == MAX_SUB_SHOTS
        r = b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "one more"})
        assert r.status_code == 422 and "the most a shot holds" in r.json()["detail"]


def test_restoring_the_plan_before_an_insert_brings_its_keyframes_back(tmp_path):
    with Board(tmp_path) as b:
        sid, one, two = _approved_two_cuts(b)
        b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "a glance"})
        b.wait()
        [new] = b.takes(sid, 2)
        b.http.post(f"/v1/shot-takes/{new['take_id']}/approve", json={"n": 0})
        version = b.http.get(f"{b.base}/{sid}/plans").json()[0]["version"]
        r = b.http.post(f"{b.base}/{sid}/plans/{version}/restore", json={})
        assert r.status_code == 200 and r.json()["keyframes_approved"] == 2
        subs = b.shot(sid)["sub_shots"]
        assert len(subs) == 2
        assert [s["approved"] for s in subs] == [[one["take_id"], 0], [two["take_id"], 0]]
        assert [s["outdated"] for s in subs] == [[], []]


def _cut(i: int, framing: str, start: int) -> SubShot:
    return SubShot(index=i, start_ms=start, end_ms=start + 2000, framing=framing, action="a")


def test_kept_cuts_holds_the_old_views():
    old = [_cut(1, "wide", 0), _cut(2, "close", 2000)]
    new = [_cut(1, "WIDER", 0), _cut(2, "insert", 1000), _cut(3, "close-up", 2500)]
    out = kept_cuts(old, new, 1)
    assert [c.framing for c in out] == ["wide", "insert", "close"]
    assert [c.start_ms for c in out] == [0, 1000, 2500]
    assert [new_index(i, 1) for i in (1, 2)] == [1, 3]
    with pytest.raises(ValueError, match="not 3"):
        kept_cuts(old, new[:2], 1)
    with pytest.raises(ValueError, match="repeats the view"):
        kept_cuts(old, [new[0], _cut(2, "wide", 1000), new[2]], 1)


def test_a_moved_keyframe_holds_only_while_its_own_keyframe_is_approved(tmp_path):
    with Board(tmp_path) as b:
        sid, _, two = _approved_two_cuts(b)
        b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "a glance", "draw": False})
        b.wait()
        assert b.shot(sid)["sub_shots"][2]["approved"] == [two["take_id"], 0]
        # Another keyframe approved for cut 1: cut 3 was drawn from the old one, so it goes.
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        other = b.takes(sid)[-1]
        r = b.http.post(f"/v1/shot-takes/{other['take_id']}/approve", json={"n": 0, "next": False})
        assert r.status_code == 200
        subs = b.shot(sid)["sub_shots"]
        assert subs[0]["approved"] == [other["take_id"], 0] and subs[2]["approved"] is None


def test_undo_and_redo_of_an_insert_move_only_the_keyframes_it_moved(tmp_path):
    with Board(tmp_path) as b:
        sid, one, two = _approved_two_cuts(b)
        b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "a glance"})
        b.wait()
        [new] = b.takes(sid, 2)
        b.http.post(f"/v1/shot-takes/{new['take_id']}/approve", json={"n": 0, "next": False})
        b.http.post(f"{b.base}/{sid}", json={"sub_shot": 3, "draws": 1})
        b.wait()
        redrawn = b.takes(sid, 3)[-1]
        b.http.post(f"/v1/shot-takes/{redrawn['take_id']}/approve", json={"n": 0})
        # Undo: cut 2 is the keyframe drawn before the insert, not the one drawn from the
        # inserted cut since.
        before = b.http.get(f"{b.base}/{sid}/plans").json()[0]["version"]
        b.http.post(f"{b.base}/{sid}/plans/{before}/restore", json={})
        subs = b.shot(sid)["sub_shots"]
        assert [s["approved"] for s in subs] == [[one["take_id"], 0], [two["take_id"], 0]]
        assert [t["take_id"] for t in subs[1]["takes"]] == [two["take_id"]]
        # Redo: the inserted plan comes back with its keyframes.
        again = b.http.get(f"{b.base}/{sid}/plans").json()[0]["version"]
        r = b.http.post(f"{b.base}/{sid}/plans/{again}/restore", json={})
        assert r.status_code == 200 and r.json()["keyframes_approved"] == 3
        subs = b.shot(sid)["sub_shots"]
        assert [s["approved"] for s in subs] == [
            [one["take_id"], 0],
            [new["take_id"], 0],
            [redrawn["take_id"], 0],
        ]


def test_nothing_starts_or_is_approved_while_the_shot_is_planned(tmp_path):
    with Board(tmp_path) as b:
        sid, one, _ = _approved_two_cuts(b)
        plan = b.repo.get_shot_plan(b.pid, b.board()["revision_id"], sid)
        b.repo.save_shot_plan(plan.model_copy(update={"status": "writing"}))
        r = b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
        assert r.status_code == 409 and "being planned" in r.json()["detail"]
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        assert r.status_code == 409 and "being planned" in r.json()["detail"]


def test_the_offline_planner_fits_a_cut_into_a_short_one():
    shot = {"people": ["p1"], "framing": "wide", "camera": "still", "action": "a"}
    cuts = [
        {"start_s": 0, "end_s": 1.5, "framing": "wide", "camera": "still", "action": "a"},
        {"start_s": 1.5, "end_s": 6, "framing": "close", "camera": "still", "action": "b"},
    ]
    context = {"shot": shot, "current_plan": cuts, "insert_after_cut": 1, "insert_note": "n"}
    out = fixture_plan(GenerationRequest(stage=PLAN_STAGE, instructions="", context=context))
    assert [c.start_s for c in out.sub_shots] == [0, 2, 4]
    assert [c.framing for c in out.sub_shots] == ["wide", "Insert shot: n", "close"]
