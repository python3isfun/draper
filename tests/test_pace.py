"""A story's pace (pace.py): the storyboard's and the planner's rules, the listener check,
the setting on the story, and the shot specs' readiness and reference limit."""

import pytest
from pydantic import ValidationError

from drama_agent.models.request import JobOptions, JobRequest
from drama_agent.pace import as_pace, planner_rules, storyboard_rules
from drama_agent.pipeline.steps import ReviewStep, StoryboardStep
from drama_agent.shot_frames import check_plan, make_sub_shots, plan_instructions, spread
from drama_agent.shots.plans import OFF_SCREEN_LINE

from .test_shot_frames import BODY, Board, TwoCuts, _specs, busiest, two_cuts, two_speakers


def test_a_story_is_brisk_unless_it_says_otherwise():
    assert JobOptions().pace == "brisk" and JobOptions(pace=None).pace == "brisk"
    assert JobOptions(pace="Slow burn").pace == "slow_burn"
    assert JobOptions(pace="slow-burn").pace == "slow_burn"
    with pytest.raises(ValidationError):
        JobOptions(pace="zippy")
    assert as_pace("zippy") == "brisk" and as_pace("MEASURED") == "measured"


@pytest.mark.parametrize(("pace", "hold", "words"), [
    ("brisk", "1.5 s", "45 to 70 words"),
    ("measured", "2.5 s", "40 to 60 words"),
    ("slow_burn", "4 s", "35 to 55 words"),
])  # fmt: skip
def test_each_pace_sets_the_numbers(pace, hold, words):
    board, plan = storyboard_rules(pace), planner_rules(pace)
    assert words in board and f"at most {hold}" in plan
    # The rules every pace holds.
    assert "physical business" in board and "never only" in board and "locked off" in board
    assert "The long setup goes to whoever acts or speaks" in plan
    assert "A listener has something to do" in plan and "Move the camera" in plan


def test_the_storyboard_is_told_continuity_across_neighbouring_shots():
    board = StoryboardStep().directive
    assert "the one before and after it" in board
    assert "must have been picked up" in board and "from the first frame" in board
    assert "behind each person" in board
    # The review checks the same continuity, and pacing at the story's pace.
    review = ReviewStep()
    assert '"shot_continuity"' in review.directive and "behind each person" in review.directive
    rules = review.rules(JobRequest(sources=BODY["sources"], options={"pace": "measured"}))
    assert '"pacing"' in rules and "measured pace" in rules
    assert "(the pace asks for at least half of them)" in rules


def test_the_storyboard_is_told_how_to_cover_a_scene():
    rules = storyboard_rules("brisk")
    assert "Shots are 3 to 6 s" in rules and "medium close-up is the default size" in rules
    assert "Begin each `framing` with its size and subject" in rules
    assert "at most one shot in two is a two-shot or group shot" in rules
    review = ReviewStep().rules(JobRequest(sources=BODY["sources"], options={"pace": "brisk"}))
    assert "shots over 8 s" in review


def test_the_camera_rule_says_the_share_of_shots():
    assert "Move the camera in at least one in three of the sub-shots" in planner_rules("slow_burn")
    assert "Move the camera in at least three in four of the shots" in storyboard_rules("brisk")


def test_the_planner_is_told_the_pace():
    assert "## Pace: brisk" in plan_instructions()
    assert "at most 4 s" in plan_instructions("slow_burn")
    assert "a short reaction can follow" in plan_instructions()


def _listening(package, shot, on_second):
    """A wide on everyone for the first line, then a single of `on_second` while Maya says
    the second line (8-11 s), whoever is on screen."""
    sides = spread(shot.character_ids)
    return make_sub_shots(package, shot, [
        {"start_ms": 0, "framing": "wide", "action": "x", "people": shot.character_ids,
         "placement": sides},
        {"start_ms": 7000, "framing": "close-up", "action": "y", "people": [on_second],
         "placement": {on_second: sides[on_second]},
         "off_screen": {p: "left" for p in shot.character_ids if p != on_second}},
    ], strict=False)  # fmt: skip


def test_a_line_spoken_off_screen_while_someone_is_in_frame_is_refused(package):
    shot = two_speakers(package)  # Ross 1-6.18 s, Maya 8-11 s, in a 15 s clip
    subs = _listening(package, shot, "ch_ross")
    [bad] = [p for p in check_plan(package, shot, subs, "brisk") if p.startswith(OFF_SCREEN_LINE)]
    assert "Maya" in bad and "sub-shot 2" in bad and "Ross is in frame" in bad
    assert "lip-syncs" in bad
    # Whatever the pace, and refused on a save: H3 can't render it.
    assert any(p.startswith(OFF_SCREEN_LINE) for p in check_plan(package, shot, subs))
    from drama_agent.shot_frames import advisory

    assert not advisory(bad)
    # The speaker on screen for her line is fine.
    ok = _listening(package, shot, "ch_maya")
    assert not any(p.startswith(OFF_SCREEN_LINE) for p in check_plan(package, shot, ok, "brisk"))


def test_a_silent_single_or_a_voice_from_outside_is_not_refused(package):
    shot = two_speakers(package)
    silent = shot.model_copy(update={"dialogue_cues": shot.dialogue_cues[:1]})
    subs = _listening(package, silent, "ch_ross")
    assert not any(
        p.startswith(OFF_SCREEN_LINE) for p in check_plan(package, silent, subs, "brisk")
    )
    # Maya's line as a voice from outside the shot (a phone): she isn't one of its people.
    away = shot.model_copy(update={"character_ids": ["ch_ross", "ch_jess"]})
    subs = make_sub_shots(package, away, [
        {"start_ms": 0, "framing": "wide", "action": "x", "people": ["ch_ross", "ch_jess"],
         "placement": spread(["ch_ross", "ch_jess"])},
        {"start_ms": 7000, "framing": "close-up", "action": "y", "people": ["ch_ross"],
         "placement": {"ch_ross": "left"}, "off_screen": {"ch_jess": "right"}},
    ], strict=False)  # fmt: skip
    assert not any(p.startswith(OFF_SCREEN_LINE) for p in check_plan(package, away, subs, "brisk"))
    # An empty frame (an insert) while someone speaks is fine too: no face to lip-sync.
    subs = make_sub_shots(package, shot, [
        {"start_ms": 0, "framing": "wide", "action": "x", "people": shot.character_ids,
         "placement": spread(shot.character_ids)},
        {"start_ms": 7000, "framing": "insert of the letter", "action": "y", "people": [],
         "off_screen": dict.fromkeys(shot.character_ids, "left")},
    ], strict=False)  # fmt: skip
    assert not any(p.startswith(OFF_SCREEN_LINE) for p in check_plan(package, shot, subs))


def test_the_pace_is_chosen_with_the_story_and_changed_on_it(tmp_path):
    body = {**BODY, "options": {"pace": "measured"}}
    with Board(tmp_path, text=TwoCuts()) as b:
        url = f"/v1/drama-packages/{b.pid}/pace"
        # The Board's story is brisk (the default): its storyboard was told so.
        [board] = [c for c in b.text.calls if c.stage == "storyboard"]
        assert "Pace: brisk." in board.instructions
        assert b.http.get(url).json() == {"pace": "brisk"}
        r = b.http.put(url, json={"pace": "slow_burn"})
        assert r.status_code == 200 and b.http.get(url).json() == {"pace": "slow_burn"}
        assert b.http.put(url, json={"pace": "zippy"}).status_code == 422
        assert b.http.get("/v1/drama-packages/nope/pace").status_code == 404
        # The next cut plan follows it.
        b.design()
        sid = busiest_id(b)
        b.http.post(f"{b.base}/{sid}/plan", json={})
        b.wait()
        assert "## Pace: slow burn" in b.calls("shot_plan")[-1].instructions
        # A new story chosen measured is measured.
        from .conftest import wait_job

        job = wait_job(b.http, b.http.post("/v1/drama-jobs", json=body).json()["job_id"])
        assert b.http.get(f"/v1/drama-packages/{job['package_id']}/pace").json() == {
            "pace": "measured"
        }


def busiest_id(b) -> str:
    return b.busiest()["shot_id"]


# ---------------------------------------------------------------------------
# The shot specs: references within the model's limit, and how far the keyframe pass is


def test_the_references_are_cut_to_nine_in_order(package, monkeypatch):
    import drama_agent.shots.clip_refs as specs

    monkeypatch.setattr(specs, "VIDEO_MAX_REFERENCES", 3)
    shot = busiest(package)
    out = _specs(package, [two_cuts(package, shot)],
                 {(shot.shot_id, 1): "a.jpg", (shot.shot_id, 2): "b.jpg"})  # fmt: skip
    mine = next(s for s in out["shots"] if s["shot_id"] == shot.shot_id)
    monkeypatch.setattr(specs, "VIDEO_MAX_REFERENCES", 99)
    whole = next(s for s in _specs(package, [two_cuts(package, shot)],
                                   {(shot.shot_id, 1): "a.jpg", (shot.shot_id, 2): "b.jpg"})
                 ["shots"] if s["shot_id"] == shot.shot_id)  # fmt: skip
    assert len(whole["references"]) > 3
    assert mine["references"] == whole["references"][:3]
    assert mine["references_left_out"] == whole["references"][3:]


def test_the_specs_say_how_far_the_keyframe_pass_is(package):
    from drama_agent.export import Names, fresh_readings
    from drama_agent.shot_specs import shot_specs

    from .test_shot_frames import done_images

    shot = busiest(package)
    plan = two_cuts(package, shot)
    out = _specs(package, [plan], {(shot.shot_id, 1): "a.jpg"})
    r = out["readiness"]
    assert r["shots"] == len(package.shot_list) and r["shots_ready"] == 0
    assert r["cuts_planned"] == 2 and r["cuts_with_keyframe"] == 1
    assert r["shots_planned"] == 1 and r["keyframe_work_running"] == 0
    assert r["note"].startswith("The keyframe pass is not complete: 0 of")
    # While keyframe work runs, the specs say not to render yet, and the shot isn't ready.
    images = done_images(package)
    stored = {plan.shot_id: plan}
    frames = {(shot.shot_id, 1): "a.jpg", (shot.shot_id, 2): "b.jpg"}
    exported = {i.image_id: f"{i.requirement_id}.jpg" for i in images}
    fresh = fresh_readings(package, stored, [], images)
    out = shot_specs(package, stored, frames, Names(package), fresh, images, exported,
                     working={shot.shot_id}, pace="measured")  # fmt: skip
    mine = next(s for s in out["shots"] if s["shot_id"] == shot.shot_id)
    assert not mine["ready"] and mine["keyframe_work_running"]
    assert "its keyframe work is still running" in mine["not_ready"]
    assert out["readiness"]["keyframe_work_running"] == 1
    assert "still running on 1 shot(s): don't render" in out["readiness"]["note"]
    assert out["pace"] == "measured"


def test_the_cut_plans_name_people_as_the_cast_sheet_does(package):
    """However the planner named them (first name, id), the specs use the full name."""
    from drama_agent.shot_frames import PlannedSubShot, PlanOutput, ShotPlan, planned_sub_shots

    shot = two_speakers(package)
    names = {c.character_id: c.name for c in package.story_bible.characters}
    first = {cid: names[cid].split()[0] for cid in shot.character_ids}
    out = PlanOutput(sub_shots=[
        PlannedSubShot(start_s=0, framing="wide", action="x",
                       people=[first["ch_ross"], "ch_maya", names["ch_jess"]],
                       placement={first["ch_ross"]: "left", "ch_maya": "center",
                                  names["ch_jess"]: "right"}),
        PlannedSubShot(start_s=7, framing="close-up on Maya", action="y", people=["ch_maya"],
                       placement={first["ch_maya"]: "center"},
                       off_screen={first["ch_ross"]: "left", "ch_jess": "right"}),
    ])  # fmt: skip
    subs = planned_sub_shots(package, shot, out, strict=False)
    plan = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, source="model", sub_shots=subs)  # fmt: skip
    package.shot_list = [shot if s.shot_id == shot.shot_id else s for s in package.shot_list]
    spec = next(s for s in _specs(package, [plan])["shots"] if s["shot_id"] == shot.shot_id)
    cast = {names[c] for c in shot.character_ids}
    for sub in spec["spec"]["sub_shots"]:
        assert set(sub["people"]) <= cast
        assert set(sub["placement"]) <= cast and set(sub["off_screen"]) <= cast
    assert spec["spec"]["sub_shots"][1]["off_screen"] == {
        names["ch_ross"]: "left", names["ch_jess"]: "right"}  # fmt: skip


class ListenerThenMidLine(TwoCuts):
    """The first plan holds a listener through the speaker's line (refused: H3 would put the
    line on the listener's face); the retry cuts in the middle of that line (refused too)."""

    async def generate(self, request, output_model):
        if request.stage != "shot_plan":
            return await super().generate(request, output_model)
        from drama_agent.shot_frames import PlannedSubShot, PlanOutput

        self.calls.append(request)
        people, line = request.context["shot"]["people"], request.context["lines"][0]
        speaker = line["speaker"]
        other = next(p for p in people if p != speaker)
        sides = {other: "left", speaker: "right"} | {
            p: "center" for p in people if p not in (other, speaker)
        }
        cut = round(line["end_s"] + 0.5) if request.attempt == 0 else (
            round((line["start_s"] + line["end_s"]) / 2, 1))  # fmt: skip
        return PlanOutput(sub_shots=[
            PlannedSubShot(start_s=0, framing=f"single of {other}", action="listens",
                           people=[other], placement={other: "left"},
                           off_screen={p: "right" for p in people if p != other}),
            PlannedSubShot(start_s=cut, framing="wide", action="all", people=people,
                           placement=sides),
        ], notes=f"attempt {request.attempt}")  # fmt: skip


def test_a_plan_that_holds_a_listener_through_a_line_is_sent_back_and_flagged(tmp_path):
    with Board(tmp_path, text=ListenerThenMidLine()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert len(b.calls("shot_plan")) == 2  # the listener sent it back once
        assert s["plan"]["notes"] == "attempt 0"  # the retry cut mid-line: the first stands
        # The board flags the off-screen line; the plan stays until a person fixes it.
        assert any(p.startswith(OFF_SCREEN_LINE) for p in s["plan"]["problems"])


def test_the_pace_api_takes_any_spelling(tmp_path):
    with Board(tmp_path) as b:
        url = f"/v1/drama-packages/{b.pid}/pace"
        assert b.http.put(url, json={"pace": "Slow burn"}).json() == {"pace": "slow_burn"}
        assert b.http.put(url, json={"pace": "measured"}).json() == {"pace": "measured"}


def test_no_shots_are_not_all_ready():
    from drama_agent.shot_specs import readiness

    r = readiness([])
    assert r["shots"] == 0 and "no shots" in r["note"] and "Every shot" not in r["note"]


def test_ratings_changed_before_story_settings_are_kept(tmp_path):
    import sqlite3

    from drama_agent.design.setup import story_rating
    from drama_agent.store.sqlite import SqliteRepository

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE story_ratings (package_id TEXT PRIMARY KEY, rating TEXT)")
        db.execute("INSERT INTO story_ratings VALUES ('pkg_1', 'R')")
    repo = SqliteRepository(path)
    assert story_rating(repo, "pkg_1") == "R"
    repo.set_story_setting("pkg_1", "pace", "measured")
    from drama_agent.pace import story_pace

    assert story_pace(repo, "pkg_1") == "measured" and story_rating(repo, "pkg_1") == "R"
    repo.close()
    # Opening it again doesn't bring the old rating back over a newer one.
    repo = SqliteRepository(path)
    repo.set_story_setting("pkg_1", "rating", "PG")
    repo.close()
    assert story_rating(SqliteRepository(path), "pkg_1") == "PG"
