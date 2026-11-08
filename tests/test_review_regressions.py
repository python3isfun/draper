"""Regression tests for the code-review findings (one test per finding)."""

from drama_agent.fixtures import sample
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Issue
from drama_agent.pipeline.impact import analyze_impact
from drama_agent.store import InMemoryRepository
from drama_agent.validation import run_hard_checks

from .conftest import run_pipeline


def failed_checks(pkg, options) -> set[str]:
    return {i.check for i in run_hard_checks(pkg, options)}


# 1. Unknown IDs from the model are repaired via retry, not a crash.
def typo_look(output, attempt):
    if attempt == 0:
        output.shots[0].character_look_refs[0].look_id = "lk_typo"
    return output


def always_typo_look(output, attempt):
    output.shots[0].character_look_refs[0].look_id = "lk_typo"
    return output


def test_unknown_look_id_is_retried_not_crashed():
    job, _ = run_pipeline(FixtureModelClient(faults={"storyboard": typo_look}))
    assert job.status == "completed"
    assert job.stages["storyboard"].attempts == 2


def test_persistent_unknown_look_id_ends_needs_revision():
    job, _ = run_pipeline(FixtureModelClient(faults={"storyboard": always_typo_look}))
    assert job.status == "needs_revision"
    assert any(i.check == "ref" for i in job.stages["storyboard"].issues)


# 2. Overlap with a long earlier line is detected even past the immediate neighbour.
def test_overlap_with_non_adjacent_line(package, options):
    a, b, c = package.timing[0].dialogue_schedule[:3]
    a.start_ms, a.end_ms = 0, 10_000
    b.start_ms, b.end_ms, b.overlap_allowed = 1_000, 2_000, True
    c.start_ms, c.end_ms = 3_000, 4_000
    assert "dialogue_overlap" in failed_checks(package, options)


# 3. Failures never leave the job or a stage in 'running'.
class FailingSaveRepo(InMemoryRepository):
    def save_package(self, pkg, published):
        raise RuntimeError("disk full")


def test_failure_in_final_save_marks_job_failed():
    import asyncio

    from drama_agent.models import Job
    from drama_agent.pipeline.orchestrator import Orchestrator

    repo = FailingSaveRepo()
    job = Job(job_id="job_x", request=sample.sample_request())
    repo.save_job(job)
    job = asyncio.run(Orchestrator(repo, FixtureModelClient()).run_job("job_x"))
    assert job.status == "failed"
    assert "disk full" in (job.failure_reason or "")


def test_exception_in_step_marks_stage_failed():
    def boom(output, attempt):
        raise RuntimeError("model timeout")

    job, _ = run_pipeline(FixtureModelClient(faults={"script": boom}))
    assert job.status == "failed"
    assert job.stages["script"].status == "failed"
    assert job.stages["script"].error == "RuntimeError: model timeout"


# 4. Duplicate timing records for one episode are rejected.
def test_duplicate_timing_record(package, options):
    package.timing.append(package.timing[0].model_copy(deep=True))
    assert "timing_record" in failed_checks(package, options)


# 5. Drafts do not replace the published revision.
def test_draft_does_not_replace_published(package):
    repo = InMemoryRepository()
    repo.save_package(package, published=True)
    draft = package.model_copy(deep=True)
    draft.revision_id = "rev_draft"
    repo.save_package(draft, published=False)
    assert repo.get_package(package.package_id).revision_id == package.revision_id
    assert repo.get_package(package.package_id, "rev_draft").revision_id == "rev_draft"


def test_never_published_package_falls_back_to_draft(package):
    repo = InMemoryRepository()
    repo.save_package(package, published=False)
    assert repo.get_package(package.package_id).revision_id == package.revision_id


# 6. Prop targets reach the scenes that use the prop.
def test_prop_target_impacts_scenes_using_it(package):
    impact = analyze_impact(package, ["prop_photo"], [])
    assert {"sc_01_01", "sc_03_01"} <= set(impact.impacted_ids)


# 7. Locked objects inside the scope are reported (but are not conflicts).
def test_locked_object_in_scope_is_reported(package):
    impact = analyze_impact(package, ["ch_zhou"], ["dl_01_02_01", "dl_01_01_01"])
    assert impact.conflicts == []
    assert impact.locked_in_scope == ["dl_01_02_01"]  # sc_01_01 precedes ch_zhou's entrance


# 8. An episode with no scenes does not crash impact analysis.
def test_episode_target_with_no_scenes(package):
    package.episodes[1].scenes = []
    impact = analyze_impact(package, ["ep_02"], [])
    assert "sc_03_01" in impact.impacted_ids


# 9. IDs must be unique across kinds.
def test_id_shared_by_scene_and_shot(package, options):
    package.shot_list[0].shot_id = "sc_01_01"
    assert "unique_ids" in failed_checks(package, options)


# 10. Review runs once and creative issues are advisory, not a gate.
def test_review_runs_once_and_does_not_gate(monkeypatch):
    from drama_agent.pipeline import steps

    calls = []
    real = steps.hc.run_hard_checks
    monkeypatch.setattr(
        steps.hc, "run_hard_checks", lambda *a, **k: calls.append(1) or real(*a, **k)
    )

    def blocker_note(output, attempt):
        output.issues.append(Issue(check="creative", object_id="ep_02", evidence="slow middle"))
        return output

    client = FixtureModelClient(faults={"review": blocker_note})
    job, repo = run_pipeline(client)
    assert job.status == "completed"
    assert [c.stage for c in client.calls].count("review") == 1
    assert len(calls) == 1  # storyboard gate only; review does not re-run the suite
    pkg = repo.get_package(job.package_id)
    assert pkg.qa_report.hard_issues == []
    assert [i.object_id for i in pkg.qa_report.creative_issues] == ["ep_02"]
