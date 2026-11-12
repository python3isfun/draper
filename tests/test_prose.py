"""Prose hygiene: no IDs, field names or timecodes in human-facing text.

Cases are taken from the live gpt-6.1-sol run that motivated this (see validation/prose.py).
"""

from drama_agent.fixtures import sample
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Beat, DialogueLine
from drama_agent.pipeline.steps import OUTPUT_RULES_VERSION, PROSE_RULES, StageNotes
from drama_agent.validation import run_hard_checks
from drama_agent.validation.prose import (
    entity_names,
    find_prose_leaks,
    normalize_prose,
    normalize_text,
)

from .conftest import run_pipeline

NAMES = entity_names(sample.sample_story_bible())


# ---------------------------------------------------------------------------
# normalize_text


def test_strips_leading_timeline_prefix():
    raw = "Shot [0,10000)ms: The glass door swings open."
    assert normalize_text(raw, NAMES) == "The glass door swings open."
    assert normalize_text("[0, 10000)ms: The door opens.", NAMES) == "The door opens."


def test_replaces_ids_glued_to_other_text():
    # The lookbehind is ASCII-only, so an ID right after punctuation is still found.
    raw = "Maya opens prop_photo—then looks at ch_frank."
    assert normalize_text(raw, NAMES) == (
        "Maya opens the old family photo—then looks at Frank Collins."
    )


def test_longest_id_wins():
    raw = "Photo state: ps_photo_torn"
    assert normalize_text(raw, NAMES) == "Photo state: Torn in half during the argument"


def test_converts_bare_durations_and_episode_labels():
    assert normalize_text("a 2300ms breath", NAMES) == "a 2.3s breath"
    assert normalize_text("hold 1500 ms", NAMES) == "hold 1.5s"
    assert normalize_text("ep_01: she sees the scar.", NAMES) == ("Episode 1: she sees the scar.")
    assert normalize_text("ep_02: denial", NAMES) == "Episode 2: denial"


def test_leaves_mid_text_spans_for_the_model():
    """A span inside a sentence is meta-commentary; rewriting it needs the model."""
    raw = "This episode's timeline is [0,120000)ms."
    assert normalize_text(raw, NAMES) == raw
    assert find_prose_leaks(_beat(raw))


def test_unknown_ids_are_not_invented():
    assert normalize_text("picks up prop_unknown", NAMES) == "picks up prop_unknown"


# ---------------------------------------------------------------------------
# Field selection


def _beat(action: str) -> Beat:
    return Beat(
        beat_id="bt_01_01_01",
        action=action,
        dialogue=DialogueLine(dialogue_id="dl_01_01_01", character_id="ch_maya", text="Hi."),
    )


def test_id_fields_and_tooling_notes_are_untouched():
    beat = _beat("Shot [0,5000)ms: Maya picks up prop_photo.")
    assert normalize_prose(beat, NAMES) == 1
    assert beat.action == "Maya picks up the old family photo."
    assert beat.beat_id == "bt_01_01_01" and beat.dialogue.character_id == "ch_maya"

    shots, _ = sample.sample_storyboard()
    shots[0].generation_notes = "reuse ar_location_loc_shop_ls_shop_day; 1500ms hold"
    normalize_prose(shots, NAMES)
    assert shots[0].generation_notes == "reuse ar_location_loc_shop_ls_shop_day; 1500ms hold"
    assert find_prose_leaks(shots) == []


def test_flags_field_names_and_reports_owner():
    beat = _beat("Not read aloud yet, spoken_timing_verified=false")
    [issue] = find_prose_leaks(beat)
    assert issue.check == "prose_leak" and issue.object_id == "bt_01_01_01"
    assert "spoken_timing_verified" in issue.evidence


def test_clean_sample_has_no_leaks(package):
    assert find_prose_leaks(package.story_bible, package.episodes, package.shot_list) == []


# ---------------------------------------------------------------------------
# Hard checks


def test_invented_map_keys_are_hard_issues(package, options):
    package.story_bible.character_arcs["planning"] = "format and runtime"
    package.episodes[2].end_state.knowledge["_production_notes"] = ["assumption: ..."]
    package.episodes[0].scenes[0].end_state.possessions["ch_ghost"] = ["the old family photo"]
    evidence = [i.evidence for i in run_hard_checks(package, options) if i.check == "entity_key"]
    assert any("'planning'" in e for e in evidence)
    assert any("'_production_notes'" in e for e in evidence)
    assert any("'ch_ghost'" in e for e in evidence)


def test_possessions_are_prose_not_prop_ids(package, options):
    """Regression: the live gpt-6.1-sol script described held items in prose, which is
    valid; an earlier version of the check demanded prop IDs and failed 89 times."""
    package.episodes[0].end_state.possessions["ch_maya"] = [
        "The cash envelope is back in his inside pocket; $3,000 still unpaid.",
        "The old family photo is still on the back wall of the front room.",
    ]
    assert run_hard_checks(package, options) == []


def test_prose_leak_is_a_hard_issue(package, options):
    package.episodes[0].main_conflict += " spoken_timing_verified=false"
    assert "prose_leak" in {i.check for i in run_hard_checks(package, options)}


# ---------------------------------------------------------------------------
# Pipeline behaviour


def leaky_script(output, attempt):
    beat = output.episodes[0].scenes[0].beats[0]
    beat.action = f"Shot [0,15000)ms: {beat.action} Maya touches prop_photo."
    return output


def test_fixable_leaks_are_cleaned_without_a_model_retry():
    job, repo = run_pipeline(FixtureModelClient(faults={"script": leaky_script}))
    assert job.status == "completed"
    assert job.stages["script"].attempts == 1  # no extra (slow, paid) model call
    action = repo.get_package(job.package_id).episodes[0].scenes[0].beats[0].action
    assert action.startswith("Derek Ross pushes through the door")
    assert action.endswith("Maya touches the old family photo.")


def meta_once(output, attempt):
    if attempt == 0:
        output.episodes[0].main_conflict += " (this episode's timeline is [0,120000)ms)"
    return output


def test_unfixable_leak_is_sent_back_for_repair():
    client = FixtureModelClient(faults={"script": meta_once})
    job, _ = run_pipeline(client)
    assert job.status == "completed" and job.stages["script"].attempts == 2
    retry = [c for c in client.calls if c.stage == "script"][1]
    assert any(i.check == "prose_leak" for i in retry.feedback)


def test_notes_are_kept_and_not_duplicated_on_retry():
    def with_notes(output, attempt):
        output.notes = StageNotes(
            assumptions=[f"assumption from attempt {attempt}"],
            open_questions=["When does Maya's mother come back to town?"],
        )
        return meta_once(output, attempt)

    job, repo = run_pipeline(FixtureModelClient(faults={"script": with_notes}))
    pkg = repo.get_package(job.package_id)
    assert pkg.assumptions == ["assumption from attempt 1"]  # replaced, not appended
    assert pkg.open_questions == ["When does Maya's mother come back to town?"]


def test_prompt_carries_prose_rules_and_version():
    client = FixtureModelClient()
    job, repo = run_pipeline(client)
    assert all(PROSE_RULES in c.instructions for c in client.calls)
    versions = repo.get_package(job.package_id).provenance.prompt_versions
    assert versions["output_rules"] == OUTPUT_RULES_VERSION
