"""A set redesigned after keyframes exist: what goes out of date, and redrawing it in order.

A hand edit to a set's text (sheet_edit) makes its pictures out of date; the pictures drawn
from them (its other states, its plan views) wait for them; the approved keyframes drawn
from them are out of date too, and the export says the shots aren't ready. Redrawing goes
in order: the set's first wide view, then what is drawn from it, then the keyframes.
"""

import json
import time

from drama_agent.images import (
    PICTURE_OUTDATED,
    PICTURE_REPLACED,
    TEXT_CHANGED,
    outdated_pictures,
)
from drama_agent.shot_frames import _cached, design_print
from drama_agent.shot_reading import fingerprint

from .test_shot_frames import Board, member

NEW_ROOM = "Low charcoal mohair sofa against the left wall; king-size bed in a walnut surround"


def picks(b: Board) -> dict:
    return b.http.get(f"/v1/drama-packages/{b.pid}/images").json()


def images_idle(b: Board, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while (v := picks(b))["queued"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return v


def location_of(b: Board, shot_id: str) -> str:
    pkg = b.repo.get_package(b.pid)
    return next(s.location_id for s in pkg.shot_list if s.shot_id == shot_id)


def set_field(b: Board, location_id: str) -> str:
    pkg = b.repo.get_package(b.pid)
    loc = next(x for x in pkg.set_sheet.locations if x.location_id == location_id)
    return "key_furnishings" if hasattr(loc, "key_furnishings") else "layout"


def edit_set(b: Board, location_id: str, text: str = NEW_ROOM):
    r = b.http.patch(
        f"/v1/drama-packages/{b.pid}/entries/{location_id}",
        json={"revision_id": b.repo.get_package(b.pid).revision_id,
              "sheet": {set_field(b, location_id): text}},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()


def by_image(v: dict) -> dict:
    return {i["image_id"]: i for i in v["images"]}


def test_nothing_is_out_of_date_before_an_edit(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        assert picks(b)["outdated"] == {}
        sid = b.board()["shots"][0]["shot_id"]
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0, "next": False})
        assert b.shot(sid)["sub_shots"][0]["outdated"] == []


def test_a_redesigned_set_is_redrawn_in_order_and_its_keyframes_follow(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.board()["shots"][0]["shot_id"]
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0, "next": False})
        loc = location_of(b, sid)

        edit_set(b, loc)
        v = picks(b)
        imgs = by_image(v)
        mine = {k: why for k, why in v["outdated"].items() if loc in imgs[k]["requirement_id"]}
        assert mine, "the set's pictures are out of date"
        assert all(TEXT_CHANGED in why for why in mine.values())
        # What is drawn from the set's first picture waits for it.
        waiting = [k for k, why in mine.items() if PICTURE_OUTDATED in why]
        roots = [k for k, why in mine.items() if PICTURE_OUTDATED not in why]
        assert roots and waiting
        # The keyframe drawn from it is out of date, and the export says the shot isn't ready.
        sub = b.shot(sid)["sub_shots"][0]
        assert any(w.startswith(PICTURE_OUTDATED) for w in sub["outdated"])
        specs = json.loads(member(b.zip(), "03_storyboard/shot_specs.json"))
        spec = next(s for s in specs["shots"] if s["shot_id"] == sid)
        assert not spec["ready"] and any("out of date" in r for r in spec["not_ready"])
        # A keyframe can't be redrawn before the pictures it is drawn from.
        r = b.http.post(b.base, json={"outdated": True, "draws": 1})
        assert (
            r.json()["started"] == []
            and "design pictures first" in r.json()["skipped"][0]["reason"]
        )

        # Redraw: the first wide view now; the rest follow as each replaces its pick.
        r = b.http.post(f"/v1/drama-packages/{b.pid}/images/refresh")
        assert r.status_code == 202 and r.json()["notes"]
        v = images_idle(b)
        assert not any(loc in by_image(v)[k]["requirement_id"] for k in v["outdated"])
        redrawn = [i for i in v["images"] if i.get("refreshes")]
        assert {i["refreshes"] for i in redrawn} >= set(mine)
        # Each redraw finished and is the pick now.
        assert all(i["status"] == "done" for i in redrawn)
        current = {(i["requirement_id"], i["deliverable_id"]): i["image_id"] for i in v["images"]
                   if i["selected"]}  # fmt: skip
        assert all(current.get((i["requirement_id"], i["deliverable_id"]), i["image_id"])
                   == i["image_id"] for i in redrawn)  # fmt: skip

        # The keyframe is now out of date because its set picture was replaced: redraw it.
        sub = b.shot(sid)["sub_shots"][0]
        assert any(w.startswith(PICTURE_REPLACED) for w in sub["outdated"])
        r = b.http.post(b.base, json={"outdated": True, "draws": 1})
        [new_id] = r.json()["started"]
        b.wait()
        new = next(t for t in b.takes(sid) if t["take_id"] == new_id)
        assert new["design_print"]
        b.http.post(f"/v1/shot-takes/{new_id}/approve", json={"n": 0, "next": False})
        assert b.shot(sid)["sub_shots"][0]["outdated"] == []


def test_approving_a_redrawn_cut_redraws_the_next_one(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [one] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})
        [two] = b.wait() and b.takes(sid, 2)
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0, "next": False})
        # A new first cut is approved: the second, drawn from the old one, is redrawn.
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        again = b.takes(sid)[-1]
        b.http.post(f"/v1/shot-takes/{again['take_id']}/approve", json={"n": 0})
        b.wait()
        second = b.shot(sid)["sub_shots"][1]
        assert len(second["takes"]) == 2
        b.http.post(f"/v1/shot-takes/{second['takes'][-1]['take_id']}/approve", json={"n": 0})
        assert b.shot(sid)["sub_shots"][1]["outdated"] == []


def test_your_own_wording_after_an_edit_is_not_out_of_date(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.board()["shots"][0]["shot_id"]
        loc = location_of(b, sid)
        v = picks(b)
        opt = next(o for o in v["options"] if loc in o["requirement_id"]) if v["options"] else None
        if opt is None:  # designs drawn without takes: explore one
            b.http.post(f"/v1/drama-packages/{b.pid}/images/explore",
                        json={"takes": 1, "draws": 1, "scope": ["sets"]})  # fmt: skip
            v = images_idle(b)
            opt = next(o for o in v["options"] if loc in o["requirement_id"])
        edit_set(b, loc)
        r = b.http.post(f"/v1/image-options/{opt['option_id']}/edit",
                        json={"prompt": opt["prompt"] or "An empty room. The image contains "
                              "no text, letters or numbers.", "draws": 1})  # fmt: skip
        assert r.status_code == 202, r.text
        v = images_idle(b)
        mine = [i for i in v["images"] if i.get("option_id") == r.json()["option_id"]]
        assert mine and all(i["brief"]["requirement_brief"] for i in mine)
        pkg = b.repo.get_package(b.pid)
        imgs = b.repo.list_images(pkg.package_id, pkg.revision_id)
        b.http.post(f"/v1/generated-images/{mine[0]['image_id']}/select")
        imgs = b.repo.list_images(pkg.package_id, pkg.revision_id)
        assert TEXT_CHANGED not in outdated_pictures(pkg, imgs).get(mine[0]["image_id"], [])


def test_a_look_edit_makes_the_scene_reading_stale(package):
    shot = next(s for s in package.shot_list if s.character_ids)
    before = fingerprint(package, shot.scene_id, {}, [])
    look = next(lk for lk in package.cast_sheet.looks if lk.character_id in shot.character_ids)
    look.wardrobe = "A long grey raincoat"
    assert fingerprint(package, shot.scene_id, {}, []) != before


def test_a_design_edit_changes_what_a_keyframe_was_written_against(package):
    shot = next(s for s in package.shot_list if s.character_ids)
    before = design_print(package, shot)
    loc = next(x for x in package.set_sheet.locations if x.location_id == shot.location_id)
    loc.layout = "A different room"
    assert design_print(package, shot) != before


def test_a_cache_never_outlives_a_hand_edit(package):
    from drama_agent.models.package import ChangeLogEntry

    first = _cached(package, "probe", lambda: "before")
    package.change_log.append(ChangeLogEntry(revision_id=package.revision_id, target_ids=["x"],
                                             changed_ids=["x"], note="edit"))  # fmt: skip
    assert first == "before" and _cached(package, "probe", lambda: "after") == "after"


def two_shots_in_a_scene(b: Board) -> tuple[str, str]:
    shots = b.board()["shots"]
    a = shots[0]
    nxt = next(s for s in shots[1:] if s["scene_id"] == a["scene_id"])
    return a["shot_id"], nxt["shot_id"]


def approve_first(b: Board, sid: str) -> dict:
    r = b.http.post(f"{b.base}/{sid}", json={"draws": 1})
    assert r.status_code == 202, r.text
    b.wait()
    take = next(t for t in b.takes(sid) if t["take_id"] == r.json()["take_id"])
    b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0, "next": False})
    return take


def test_an_out_of_date_keyframe_is_never_drawn_from(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        a, nxt = two_shots_in_a_scene(b)
        approve_first(b, a)
        second = approve_first(b, nxt)
        assert any(r["kind"] == "previous_shot" for r in second["references"])
        edit_set(b, location_of(b, a))
        b.http.post(f"/v1/drama-packages/{b.pid}/images/refresh")
        images_idle(b)
        # The batch redraws the first shot and waits on the next, naming the one to redraw.
        r = b.http.post(b.base, json={"outdated": True, "draws": 1}).json()
        started = {t["shot_id"] for s in b.board()["shots"] for x in s["sub_shots"]
                   for t in x["takes"] if t["take_id"] in r["started"]}  # fmt: skip
        assert a in started and nxt not in started
        wait = next(x for x in r["skipped"] if x["shot_id"] == nxt)
        assert a in wait["reason"] and "continues from is out of date" in wait["reason"]
        b.wait()
        # Drawn by hand meanwhile, the next shot leaves the earlier design's keyframe out.
        take = b.http.post(f"{b.base}/{nxt}", json={"draws": 1}).json()
        assert not any(x["kind"] == "previous_shot" for x in take["references"])
        b.wait()


def test_a_plan_written_before_a_design_edit_is_flagged(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        assert b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot)).status_code in (200, 202)
        assert b.shot(sid)["plan"]["outdated"] is None
        edit_set(b, location_of(b, sid))
        assert "plan the cuts again" in b.shot(sid)["plan"]["outdated"]
        # Planned again after the edit: up to date.
        b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot))
        assert b.shot(sid)["plan"]["outdated"] is None


def test_a_plan_from_before_design_prints_is_judged_by_the_edits(package):
    from datetime import timedelta

    from drama_agent.models.package import utcnow
    from drama_agent.sheet_edit import edit_entry
    from drama_agent.shot_frames import plan_outdated

    from .test_shot_frames import busiest, two_cuts

    shot = busiest(package)
    old = two_cuts(package, shot).model_copy(update={"source": "model",
                                                     "updated_at": utcnow() - timedelta(days=1)})  # fmt: skip
    assert old.design_print is None and plan_outdated(package, shot, old) is None
    edit_entry(package, shot.location_id, sheet={"layout": "A different room"})
    assert plan_outdated(package, shot, old) is not None
    newer = old.model_copy(update={"updated_at": utcnow() + timedelta(seconds=1)})
    assert plan_outdated(package, shot, newer) is None


def test_the_writer_and_the_judge_read_the_sets_layout(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.board()["shots"][0]["shot_id"]
        edit_set(b, location_of(b, sid), "The bed stands against the left wall under a canvas")
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        b.wait()
        from drama_agent.shot_frames import CHECK_STAGE, WRITE_STAGE

        for stage in (WRITE_STAGE, CHECK_STAGE):
            calls = b.calls(stage)
            assert calls, stage
            scene = calls[-1].context["scene"]
            assert "set" in scene and any("canvas" in v for v in scene["set"].values()), stage


class SlowImages:
    """Wraps the fixture image client so each draw takes a moment (time to pause)."""

    def __init__(self, inner, delay: float = 0.3):
        self.inner, self.delay = inner, delay
        self.model_id, self.max_references = inner.model_id, inner.max_references

    async def generate(self, prompt, size, seed, references):
        import asyncio

        await asyncio.sleep(self.delay)
        return await self.inner.generate(prompt, size, seed, references)


def test_a_take_can_be_paused_mid_run_and_resumed(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.board()["shots"][0]["shot_id"]
        # The runner reads its provider at draw time: slow H3 down to pause mid-take.
        runner = next(r for r in _runners(b))
        runner.providers["h3"] = SlowImages(runner.providers["h3"])
        r = b.http.post(f"{b.base}/{sid}", json={"draws": 4, "max_rounds": 1}).json()
        tid = r["take_id"]
        deadline = time.monotonic() + 10
        while not any(d["status"] == "done" for d in b.takes(sid)[0]["results"]):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert b.http.post(f"/v1/shot-takes/{tid}/pause").status_code == 200
        assert any(t["pausing"] for t in b.takes(sid))
        board = b.wait()
        take = next(t for t in b.takes(sid) if t["take_id"] == tid)
        assert board["busy"] == 0 and take["status"] == "paused"
        done = [d for d in take["results"] if d["status"] == "done"]
        assert 1 <= len(done) < 4
        # A paused take's finished draws can be approved; it isn't "being made".
        assert b.http.post(f"/v1/shot-takes/{tid}/pause").status_code == 409
        # Resumed, it draws the rest and is checked.
        assert b.http.post(f"/v1/shot-takes/{tid}/resume").status_code == 202
        b.wait()
        take = next(t for t in b.takes(sid) if t["take_id"] == tid)
        assert take["status"] == "done" and len(take["results"]) == 4
        assert all(d["check"] for d in take["results"] if d["status"] == "done")
        assert [d["seed"] for d in take["results"][: len(done)]] == [d["seed"] for d in done]


def _runners(b: Board):
    """The app's keyframe runner (found through the routes' closures)."""
    from drama_agent.shot_frames import ShotFrameRunner

    for route in b.app.routes:
        for cell in getattr(getattr(route, "endpoint", None), "__closure__", None) or ():
            try:
                v = cell.cell_contents
            except ValueError:
                continue
            if isinstance(v, ShotFrameRunner):
                yield v
                return
