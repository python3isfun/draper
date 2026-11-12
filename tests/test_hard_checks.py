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
