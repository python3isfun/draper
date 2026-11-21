"""Locking a shot to its keyframes (shots/lock.py): the approved keyframes are read, the
editor corrects the reading, and it becomes the plan; unlocking restores the draft."""

from drama_agent.shots.lock import SeenCut, locked_subs
from drama_agent.shots.models import ShotPlan, SubShot

from .test_shot_frames import Board
from .test_video_prompt import _approved_two_cuts, _specs


def _lock_ready(b):
    sid, one, two = _approved_two_cuts(b)
    b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
    b.wait()
    return sid, one, two


def test_the_phases_and_a_lock_from_the_keyframes_reading(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        assert b.shot(sid)["lock"]["state"] == "draft"
        r = b.http.post(f"{b.base}/{sid}/read-keyframes", json={})
        assert r.status_code == 409  # nothing approved yet
        sid, one, two = _lock_ready(b)
        assert b.shot(sid)["lock"]["state"] == "review"
        r = b.http.post(f"{b.base}/{sid}/read-keyframes", json={})
        assert r.status_code == 202 and r.json()["status"] == "writing"
        b.wait()
        lock = b.shot(sid)["lock"]
        reading = lock["reading"]
        assert reading["status"] == "done" and not reading["stale"]
        assert [c["index"] for c in reading["cuts"]] == [1, 2]
        call = b.calls("keyframe_reading")[-1]
        assert [m.label for m in call.media] == ["<Cut 1>", "<Cut 2>"]
        # The editor corrects cut 2 (the picture is a full-length shot) and locks.
        cuts = reading["cuts"]
        cuts[1]["framing"] = "Full-length single from the aisle at chest height"
        cuts[1]["note"] = "She holds one black pump; the window behind her."
        r = b.http.post(f"{b.base}/{sid}/lock", json={"cuts": cuts})
        assert r.status_code == 200
        plan = r.json()
        assert plan["source"] == "keyframes"
        assert plan["sub_shots"][1]["framing"].startswith("Full-length single from the aisle")
        assert "one black pump" in plan["sub_shots"][1]["framing"]
        shot = b.shot(sid)
        assert shot["lock"]["state"] == "locked"
        # The keyframes stay approved, and the video prompt is written again from the lock.
        assert [s["approved"] for s in shot["sub_shots"]] == [
            [one["take_id"], 0],
            [two["take_id"], 0],
        ]
        b.wait()
        assert b.shot(sid)["video_prompt"]["state"] == "current"
        assert "one black pump" in " ".join(
            c["framing"] for c in b.calls("video_prompt")[-1].context["cuts"]
        )
        # A locked plan can't be re-planned, edited or added to without unlocking.
        assert b.http.post(f"{b.base}/{sid}/plan", json={}).status_code == 409
        assert (
            b.http.post(f"{b.base}/{sid}/insert", json={"after": 1, "note": "x"}).status_code == 409
        )
        mine = next(s for s in _specs(b)["shots"] if s["shot_id"] == sid)
        assert mine["locked"] and mine["planned_by"] == "keyframes"
        # Unlock: the draft plan comes back with its keyframes.
        r = b.http.post(f"{b.base}/{sid}/unlock", json={})
        assert r.status_code == 200 and r.json()["keyframes_approved"] == 2
        shot = b.shot(sid)
        assert shot["plan"]["source"] == "user" and shot["lock"]["state"] == "review"
        assert shot["sub_shots"][1]["framing"] == "close-up"
        assert b.http.post(f"{b.base}/{sid}/unlock", json={}).status_code == 409


def test_a_new_keyframe_makes_the_reading_stale(tmp_path):
    with Board(tmp_path) as b:
        sid, _, _ = _lock_ready(b)
        b.http.post(f"{b.base}/{sid}/read-keyframes", json={})
        b.wait()
        b.http.post(f"{b.base}/{sid}", json={"sub_shot": 2, "draws": 1})
        b.wait()
        again = b.takes(sid, 2)[-1]
        b.http.post(f"/v1/shot-takes/{again['take_id']}/approve", json={"n": 0})
        assert b.shot(sid)["lock"]["reading"]["stale"]


def _plan(subs):
    return ShotPlan(package_id="p", revision_id="r", shot_id="s", source="user", sub_shots=subs)


def test_locked_subs_keep_timing_and_action(package):
    from .test_shot_frames import busiest, two_cuts

    shot = busiest(package)
    plan = two_cuts(package, shot)
    seen = [
        SeenCut(index=1, framing="Wide from the door", camera="static", people=shot.character_ids,
                placement=plan.sub_shots[0].placement, note="Morning light."),
        SeenCut(index=2, framing="Close-up", people=[shot.character_ids[0]],
                placement={shot.character_ids[0]: "center"}),
    ]  # fmt: skip
    subs = locked_subs(package, shot, plan, seen)
    assert [s.start_ms for s in subs] == [s.start_ms for s in plan.sub_shots]
    assert [s.action for s in subs] == [s.action for s in plan.sub_shots]
    assert subs[0].framing == "Wide from the door. Morning light."
    assert subs[1].placement == {shot.character_ids[0]: "center"}
    assert isinstance(subs[0], SubShot)
