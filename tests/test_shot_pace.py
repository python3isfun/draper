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
        b.text.fail = True
        r = b.http.post(f"{b.base}/{sid}/plan", json={"pace": "slow_burn", "cuts": 2})
        assert r.json()["pace"] == "slow_burn" and r.json()["cuts"] == 2
        s = b.wait() and b.shot(sid)
        assert s["plan"]["status"] == "failed" and len(s["sub_shots"]) == 2  # the old cuts
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
