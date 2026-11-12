"""Asset aggregation and revision impact (spec 6D, 9; acceptance cases 9, 11, 12)."""

from drama_agent.pipeline.impact import analyze_impact


def test_reuse_across_episodes(package):
    """Acceptance 9: one requirement per character look / location state across episodes."""
    reqs = package.asset_requirements.by_id()
    apron = reqs["ar_character_look_ch_maya_lk_maya_apron"]
    assert {u.episode_id for u in apron.used_by} == {"ep_01", "ep_02", "ep_03"}
    # A costume change gets its own look under the same character identity.
    rain = reqs["ar_character_look_ch_frank_lk_frank_rain"]
    home = reqs["ar_character_look_ch_frank_lk_frank_home"]
    assert rain.depends_on == home.depends_on == ["ar_character_identity_ch_frank"]


def test_voice_only_character_has_no_visual_requirement(package):
    """Acceptance 12: phone-only character counts in the cast but needs no image."""
    assert "ch_mom" in package.episodes[1].character_ids()
    assert not any(
        s.entity_id == "ch_mom" for r in package.asset_requirements.items for s in r.subject_refs
    )


def test_completed_without_any_images(package):
    """Acceptance 12: required items ready, fulfilled_by empty, still completed."""
    required = [r for r in package.asset_requirements.items if r.priority == "required"]
    assert required and all(r.requirement_status == "ready" for r in required)
    assert all(r.fulfilled_by == [] for r in required)


def test_design_override_is_applied(package):
    req = package.asset_requirements.by_id()["ar_location_loc_shop_ls_shop_night"]
    assert req.must_keep and req.design_freedom


def test_shot_reference_depends_on_its_assets(package):
    req = package.asset_requirements.by_id()["ar_shot_sh_01_02_04"]
    assert "ar_prop_prop_photo_ps_photo_intact" in req.depends_on
    assert "ar_location_loc_shop_ls_shop_night" in req.depends_on


def test_look_change_marks_look_and_shot_reference_stale(package):
    """Acceptance 11: look change invalidates its image and dependent compositions only."""
    impact = analyze_impact(package, ["lk_frank_rain"], [])
    assert impact.stale_requirement_ids == [
        "ar_character_look_ch_frank_lk_frank_rain",
        "ar_shot_sh_01_02_04",
    ]
    assert "ar_character_identity_ch_frank" not in impact.stale_requirement_ids


def test_dialogue_only_change_keeps_images(package):
    """Acceptance 11: a dialogue edit with no visual change invalidates no requirement."""
    impact = analyze_impact(package, ["dl_02_01_02"], [])
    assert impact.stale_requirement_ids == []
    assert "sh_02_01_02" in impact.impacted_ids


def test_character_story_change_reaches_later_scenes(package):
    impact = analyze_impact(package, ["ch_frank"], [])
    assert {"sc_01_02", "sc_03_02"} <= set(impact.impacted_ids)
    assert impact.stale_requirement_ids == []


def test_locked_target_is_conflict(package):
    """Acceptance 7: editing a locked object is reported, not silently done."""
    impact = analyze_impact(package, ["dl_01_01_01"], ["dl_01_01_01"])
    assert impact.conflicts == ["dl_01_01_01"]
