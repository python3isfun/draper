"""Hard checks (spec section 8): the sample passes, and each targeted break is caught."""

from drama_agent.models import Shot
from drama_agent.validation import check_locks, run_hard_checks


def checks_failed(pkg, options) -> set[str]:
    return {i.check for i in run_hard_checks(pkg, options)}


def shot(pkg, shot_id: str) -> Shot:
    return next(s for s in pkg.shot_list if s.shot_id == shot_id)


def test_sample_package_passes(package, options):
    assert run_hard_checks(package, options) == []
    assert all(t.planned_timing_valid for t in package.timing)
    assert not any(t.spoken_timing_verified for t in package.timing)


def test_wrong_scene_count(package, options):
    ep = package.episodes[0]
    ep.scenes = ep.scenes[:1]
    assert "scenes_per_episode" in checks_failed(package, options)


def test_too_few_characters(package, options):
    for sc in package.episodes[1].scenes:
        sc.character_ids = [c for c in sc.character_ids if c in ("ch_maya", "ch_frank")]
    assert "characters_per_episode" in checks_failed(package, options)


def test_timeline_gap_detected(package, options):
    shot(package, "sh_01_01_02").duration_ms -= 1000
    failed = checks_failed(package, options)
    assert {"timeline", "scene_duration"} <= failed


def test_unscheduled_dialogue(package, options):
    package.timing[0].dialogue_schedule.pop(0)
    assert "dialogue_schedule" in checks_failed(package, options)


def test_dialogue_scheduled_twice(package, options):
    sched = package.timing[0].dialogue_schedule
    sched.append(sched[0].model_copy())
    assert "dialogue_schedule" in checks_failed(package, options)


def test_spoken_timing_cannot_be_claimed(package, options):
    """Acceptance 6: no voice measurement means spoken_timing_verified must stay false."""
    package.timing[0].spoken_timing_verified = True
    assert "spoken_timing" in checks_failed(package, options)


def test_undeclared_character_in_shot(package, options):
    shot(package, "sh_01_01_01").character_ids.append("ch_stranger")
    assert "ref" in checks_failed(package, options)


def test_shot_beat_from_other_scene(package, options):
    shot(package, "sh_01_01_01").source_beat_ids = ["bt_02_01_01"]
    assert "ref" in checks_failed(package, options)


def test_used_by_must_be_bidirectional(package, options):
    shot(package, "sh_01_01_01").asset_requirement_ids.pop()
    assert "asset_used_by" in checks_failed(package, options)


def test_dependency_cycle(package, options):
    reqs = package.asset_requirements.by_id()
    identity = reqs["ar_character_identity_ch_maya"]
    identity.depends_on.append("ar_character_look_ch_maya_lk_maya_apron")
    assert "asset_depends_on" in checks_failed(package, options)


def test_required_requirement_must_be_ready(package, options):
    package.asset_requirements.items[0].requirement_status = "draft"
    assert "asset_status" in checks_failed(package, options)


def test_unmerged_duplicate_reuse_unit(package, options):
    dup = package.asset_requirements.items[0].model_copy(deep=True)
    dup.requirement_id = "ar_dup"
    dup.deliverables = []
    dup.priority = "optional"
    package.asset_requirements.items.append(dup)
    for u in dup.used_by:
        shot(package, u.shot_id).asset_requirement_ids.append("ar_dup")
    assert "asset_merge" in checks_failed(package, options)


def test_variant_must_belong_to_entity(package, options):
    req = package.asset_requirements.by_id()["ar_character_look_ch_maya_lk_maya_apron"]
    req.subject_refs[0].variant_id = "lk_ross_suit"
    assert "asset_subject" in checks_failed(package, options)


def test_locked_object_modified(package):
    base = package.model_copy(deep=True)
    base.locked_ids = ["dl_01_01_01"]
    package.episodes[0].scenes[0].beats[0].dialogue.text = "changed"
    assert [i.object_id for i in check_locks(base, package)] == ["dl_01_01_01"]


def look_issues(pkg, options) -> list[str]:
    return [i.evidence for i in run_hard_checks(pkg, options) if "look ref" in i.evidence]


def test_two_looks_in_one_shot_says_to_split_it(package, options):
    """Seen live: a shirt torn on screen got both looks in one shot, twice, because the
    feedback didn't say how to fix it."""
    sh = next(s for s in package.shot_list if s.character_look_refs)
    ref = sh.character_look_refs[0]
    sh.character_look_refs.append(ref.model_copy())
    [evidence] = look_issues(package, options)
    assert f"{ref.character_id} has 2 looks" in evidence
    assert "split the shot where the look changes" in evidence


def test_a_visible_character_without_a_look_is_named(package, options):
    sh = next(s for s in package.shot_list if s.character_look_refs)
    missing = sh.character_look_refs.pop(0).character_id
    [evidence] = look_issues(package, options)
    assert f"{missing} is visible but has no look ref" in evidence


def test_storyboard_and_review_instructions():
    from drama_agent.pipeline.steps import ADAPTATION_RULES, ReviewStep, StoryboardStep

    assert "end the shot on the change" in StoryboardStep.directive
    assert "empty source_refs are correct" in ReviewStep.directive
    assert "don't copy them there" in ADAPTATION_RULES


def test_coverage_refuses_the_same_setup_twice_and_a_scene_of_two_shots(package, options):
    from drama_agent.validation.hard_checks import is_group_shot, shot_size

    assert shot_size("Medium close-up single of Lila") == "medium close-up"
    assert shot_size("Vertical medium-wide two-shot by the bed") == "medium-wide"
    assert shot_size("Tabletop close-up of her hands") == "insert"
    assert shot_size("Over-the-shoulder on Derek") is None
    assert is_group_shot("Medium two-shot favoring Lila", 2)
    assert not is_group_shot("Medium close-up single of Lila", 2)
    assert not is_group_shot("Over-the-shoulder on Derek, Lila's shoulder frame left", 2)
    # The sample alternates its sizes: clean. Two mediums of the same people in a row: not.
    a, b = shot(package, "sh_01_01_01"), shot(package, "sh_01_01_02")
    a.framing, b.framing = "Medium two-shot of both", "Medium two-shot, closer"
    b.character_ids = list(a.character_ids)
    issues = [i for i in run_hard_checks(package, options) if i.check == "coverage"]
    assert any(
        i.object_id == b.shot_id and "same setup as sh_01_01_01" in i.evidence for i in issues
    )
    # Every shot of a scene a two-shot: the scene is flagged.
    scene = a.scene_id
    for s in package.shot_list:
        if s.scene_id == scene:
            s.framing = (
                "Medium two-shot" if s is a else "Wide two-shot" if s is b else "Close two-shot"
            )
    issues = [i for i in run_hard_checks(package, options) if i.check == "coverage"]
    assert any(i.object_id == scene and "two-shots or group shots" in i.evidence for i in issues)


def test_a_revision_is_held_to_coverage_only_where_it_rewrote_shots(package):
    from drama_agent.models import Issue
    from drama_agent.pipeline.revise import coverage_in_scope

    a = package.shot_list[0]
    issues = [Issue(check="coverage", object_id=a.shot_id, evidence="same setup"),
              Issue(check="coverage", object_id=a.scene_id, evidence="two-shots"),
              Issue(check="coverage", object_id="sh_other", evidence="same setup"),
              Issue(check="timeline", object_id="ep_01", evidence="gap")]  # fmt: skip
    kept = coverage_in_scope(issues, package, {a.shot_id})
    assert [i.object_id for i in kept] == [a.shot_id, a.scene_id, "ep_01"]
    assert [i.object_id for i in coverage_in_scope(issues, package, set())] == ["ep_01"]
