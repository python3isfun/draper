"""A shot's own pace and cut count (ShotPlan.pace, .cuts): asked for with "Plan cuts", told
to the planner, checked, and kept for the next re-plan."""

from drama_agent.shot_frames import ShotPlan, plan_instructions, plan_problems
from drama_agent.shots.plans import CUT_COUNT, ONE_CUT, advisory, cut_count_problem
from drama_agent.shots.runner import _rank

from .test_pace import _listening
from .test_shot_frames import Board, TwoCuts, _specs, two_speakers


def _instructions(b) -> str:
    return b.calls("shot_plan")[-1].instructions


def test_a_shot_is_planned_at_its_own_pace_and_cut_count(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        r = b.http.post(f"{b.base}/{sid}/plan", json={"pace": "Measured", "cuts": 3})
        assert r.status_code == 202, r.text
        s = b.wait() and b.shot(sid)
        assert "## Pace: measured" in _instructions(b)
        assert "exactly 3 sub-shots" in _instructions(b)
        # The planner cut it in two: sent back once with that problem, then the better kept.
        calls = b.calls("shot_plan")
        assert len(calls) == 2
        assert "this shot asks for exactly 3" in " ".join(calls[1].context["problems"])
        assert s["plan"]["pace"] == "measured" and s["plan"]["cuts"] == 3
        assert s["plan"]["story_pace"] == "brisk"

        # A re-plan that doesn't say keeps the shot's settings.
        b.http.post(f"{b.base}/{sid}/plan", json={})
        s = b.wait() and b.shot(sid)
        assert "## Pace: measured" in _instructions(b) and "exactly 3" in _instructions(b)
        assert s["plan"]["pace"] == "measured" and s["plan"]["cuts"] == 3

        # Null goes back to the story's pace and lets the planner decide.
        b.http.post(f"{b.base}/{sid}/plan", json={"pace": None, "cuts": None})
        s = b.wait() and b.shot(sid)
        assert "## Pace: brisk" in _instructions(b) and "## Cuts" not in _instructions(b)
        assert s["plan"]["pace"] is None and s["plan"]["cuts"] is None

        # Out of range or unknown is refused.
        assert b.http.post(f"{b.base}/{sid}/plan", json={"cuts": 6}).status_code == 422
        assert b.http.post(f"{b.base}/{sid}/plan", json={"pace": "zippy"}).status_code == 422


def test_the_planner_is_told_the_cut_count():
    assert "exactly 1 sub-shot" in plan_instructions("brisk", 1)
    assert "overrides the coverage rule" in plan_instructions("brisk", 1)
    assert "exactly 2 sub-shots" in plan_instructions("brisk", 2)
    assert "## Cuts" not in plan_instructions("brisk")
    assert cut_count_problem([1, 2], 3) == "the plan has 2 sub-shots; this shot asks for exactly 3"
    assert cut_count_problem([1, 2], 2) is None and cut_count_problem([1], None) is None


def test_a_listener_held_through_a_line_is_refused_at_any_pace(package):
    from drama_agent.shots.plans import OFF_SCREEN_LINE

    shot = two_speakers(package)  # a listener held 8 s while Maya speaks
    subs = _listening(package, shot, "ch_ross")
    plan = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, source="model", sub_shots=subs)  # fmt: skip
    bad = [p for p in plan_problems(package, shot, plan, "brisk") if p.startswith(OFF_SCREEN_LINE)]
    assert bad and "Maya" in bad[0]
    slow = plan.model_copy(update={"pace": "slow_burn"})
    assert [p for p in plan_problems(package, shot, slow, "brisk") if p.startswith(OFF_SCREEN_LINE)]


def test_the_specs_say_each_shots_pace(package):
    shot = package.shot_list[0]
    plan = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, source="user", pace="slow_burn",
                    sub_shots=_single(package, shot))  # fmt: skip
    out = {s["shot_id"]: s for s in _specs(package, [plan])["shots"]}
    assert out[shot.shot_id]["pace"] == "slow_burn"
    other = package.shot_list[1].shot_id
    assert out[other]["pace"] is None  # no story pace given to _specs: none recorded


def _single(package, shot):
    from drama_agent.shot_frames import single_plan

    return single_plan(package, shot).sub_shots


class FlakyPlanner(TwoCuts):
    """Cuts every shot in two, until `fail` is set: then planning fails."""

    fail = False

    async def generate(self, request, output_model):
        if request.stage == "shot_plan" and self.fail:
            raise RuntimeError("planner down")
        return await super().generate(request, output_model)


def test_the_asked_settings_hold_when_planning_fails(tmp_path):
    with Board(tmp_path, text=FlakyPlanner()) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={"clip_s": 8})
        s = b.wait() and b.shot(sid)
        assert s["clip_ms"] == 8000
        # Left out, the clip is the plan's own (as the pace and cut count are).
        b.http.post(f"{b.base}/{sid}/plan", json={})
        assert b.wait() and b.calls("shot_plan")[-1].context["clip_s"] == 8
        before = b.shot(sid)["sub_shots"]
        b.text.fail = True
        r = b.http.post(f"{b.base}/{sid}/plan", json={"pace": "slow_burn", "cuts": 2})
        assert r.json()["pace"] == "slow_burn" and r.json()["cuts"] == 2
        s = b.wait() and b.shot(sid)
        assert s["plan"]["status"] == "failed" and s["sub_shots"] == before  # the old cuts
        assert s["plan"]["pace"] == "slow_burn" and s["plan"]["cuts"] == 2  # for the next try


def test_your_own_cuts_replace_the_count_asked_of_the_planner(tmp_path):
    with Board(tmp_path, text=TwoCuts()) as b:
        b.design()
        shot = b.busiest()
        sid = shot["shot_id"]
        b.http.post(f"{b.base}/{sid}/plan", json={"cuts": 3})
        assert b.wait() and b.shot(sid)["plan"]["cuts"] == 3
        own = b.two_cuts(shot)
        assert b.http.post(f"{b.base}/{sid}/plan", json={**own, "cuts": 3}).status_code == 422
        r = b.http.post(f"{b.base}/{sid}/plan", json=own)
        assert r.status_code == 202, r.text
        assert r.json()["cuts"] is None and b.shot(sid)["plan"]["cuts"] is None


def test_a_wrong_cut_count_is_flagged_and_ranks_below_a_refused_plan(package):
    shot = two_speakers(package)
    plan = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, source="model", sub_shots=_single(package, shot))  # fmt: skip
    one_cut = [p for p in plan_problems(package, shot, plan) if p.startswith(ONE_CUT)]
    assert one_cut  # two people speak, held in one cut
    asked_one = plan.model_copy(update={"cuts": 1})
    # The director's one cut.
    assert not [p for p in plan_problems(package, shot, asked_one) if p.startswith(ONE_CUT)]
    asked_two = plan.model_copy(update={"cuts": 2})
    wrong = [p for p in plan_problems(package, shot, asked_two) if p.startswith(CUT_COUNT)]
    assert wrong == ["the plan has 1 sub-shots; this shot asks for exactly 2"]
    assert advisory(wrong[0])
    # A clean plan with the wrong count beats one with the right count a save would refuse.
    refused = "the cut at 1 s falls in the middle of Maya's line"
    assert _rank(wrong) < _rank([refused])
    assert _rank([]) < _rank(wrong)


def test_a_line_cut_across_shots_is_spoken_in_parts(package):
    """Each shot quotes only the words spoken in it, by the share of the line's time it
    holds; together the parts are the whole line, in order."""
    from drama_agent.shots.plans import shot_lines, split_line

    line = "You know that in the history of humankind nobody has ever regretted a good breakfast."
    assert split_line(line, [1700, 3000, 1600]) == [
        "You know that in", "the history of humankind nobody has ever", "regretted a good breakfast."]  # fmt: skip
    assert split_line(line, [6300]) == [line]
    # A cut moves to a comma or a full stop within two words of where the time falls...
    assert split_line("It should be. You tell dad jokes, but dirty.", [1600, 1600]) == [
        "It should be.", "You tell dad jokes, but dirty."]  # fmt: skip
    assert split_line("Look at the curves, the way the lines glide. Soft, like a brush.",
                      [3000, 2000]) == ["Look at the curves, the way the lines glide.",
                                        "Soft, like a brush."]  # fmt: skip
    # ...unless a part would then be spoken too fast for its seconds.
    assert split_line("Fine. I will never say that to anyone at all.", [600, 2200]) == [
        "Fine. I", "will never say that to anyone at all."]  # fmt: skip
    # A shot that holds a moment of the line has none of it: no word is spoken twice, and
    # nobody mouths one word in a tenth of a second.
    assert split_line("Point proven. No. I would be late for work.", [3600, 100]) == [
        "Point proven. No. I would be late for work.", ""]  # fmt: skip
    assert split_line("I have the perfect idea. A car!", [200, 3300]) == [
        "", "I have the perfect idea. A car!"]  # fmt: skip
    assert split_line("Hi.", [100, 50]) == ["Hi.", ""]  # spoken where most of it falls
    for text, spans in ((line, [1700, 3000, 1600]), ("One two three.", [900, 900, 900, 900]),
                        ("Soft and deliberate, like brushstrokes across a canvas.", [4000, 500])):  # fmt: skip
        assert " ".join(x for x in split_line(text, spans) if x) == text

    first = next(s for s in package.shot_list if s.dialogue_cues)
    nxt = next(s for s in package.shot_list if s.episode_id == first.episode_id
               and s.start_ms > first.start_ms)  # fmt: skip
    cue = first.dialogue_cues[-1]
    end = cue.end_ms
    cue.end_ms = first.start_ms + first.duration_ms  # the line runs over the cut
    nxt.dialogue_cues = [cue.model_copy(update={"start_ms": nxt.start_ms,
                                                "end_ms": nxt.start_ms + (end - cue.end_ms)
                                                if end > cue.end_ms else nxt.start_ms + 1500}),
                         *nxt.dialogue_cues]  # fmt: skip
    head = next(x for x in shot_lines(package, first) if x["dialogue_id"] == cue.dialogue_id)
    tail = next(x for x in shot_lines(package, nxt) if x["dialogue_id"] == cue.dialogue_id)
    assert (head["part"], tail["part"]) == ("start", "end")
    assert head["full_text"] == tail["full_text"]
    assert f"{head['text']} {tail['text']}" == head["full_text"]
    assert tail["start_ms"] == 0
    # A sliver of the line in the next shot: the first shot speaks all of it, the next none.
    nxt.dialogue_cues[0].end_ms = nxt.start_ms + 100
    [whole] = [x for x in shot_lines(package, first) if x["dialogue_id"] == cue.dialogue_id]
    assert (whole["part"], whole["text"]) == ("whole", whole["full_text"])
    assert all(x["dialogue_id"] != cue.dialogue_id for x in shot_lines(package, nxt))


def test_a_planner_answer_with_no_cuts_plans_the_shot_as_one(package):
    """A short single with one line needs no cut; a planner that answers with no sub-shots
    (rather than one covering the shot) gets the storyboard's shot as its one cut instead
    of a refused plan."""
    from drama_agent.shots.planner import PlanOutput, planned_sub_shots

    shot = package.shot_list[0]
    [only] = planned_sub_shots(package, shot, PlanOutput(sub_shots=[], notes="no cut needed"))
    assert (only.start_ms, only.framing, only.people) == (0, shot.framing, shot.character_ids)
    assert only.end_ms >= shot.duration_ms


def test_the_plan_reader_settles_the_planners_slips_by_the_storyboard(package):
    """A person the planner names who isn't in the shot (the action mentions them) is left
    out, and a cut that lands inside a line moves to the line's end (or goes, with no room
    left): the storyboard decides both."""
    from drama_agent.shots.planner import PlannedSubShot, PlanOutput, planned_sub_shots
    from drama_agent.shots.plans import shot_lines

    shot = next(s for s in package.shot_list if shot_lines(package, s)
                and shot_lines(package, s)[0]["end_ms"] - shot_lines(package, s)[0]["start_ms"]
                >= 2000)  # fmt: skip
    [line, *_] = shot_lines(package, shot)
    stranger = next(c.name for c in package.story_bible.characters
                    if c.character_id not in shot.character_ids)  # fmt: skip
    mine = next(c.name for c in package.story_bible.characters
                if c.character_id == line["speaker_id"])  # fmt: skip
    inside = (line["start_ms"] + line["end_ms"]) / 2000  # a moment inside the line, in s
    out = PlanOutput(sub_shots=[
        PlannedSubShot(start_s=0, framing="wide", action="talk", people=[mine, stranger],
                       placement={mine: "left", stranger: "right"}),
        PlannedSubShot(start_s=inside, framing="close-up", action="listens", people=[mine],
                       placement={mine: "left"}),
    ])  # fmt: skip
    subs = planned_sub_shots(package, shot, out)
    assert subs[0].people == [line["speaker_id"]]
    assert list(subs[0].placement) == [line["speaker_id"]]
    if len(subs) == 2:
        assert subs[1].start_ms == line["end_ms"]
    else:
        assert subs[0].end_ms - line["end_ms"] < 1000  # no room after the line
