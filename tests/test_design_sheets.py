"""Cast, set and prop sheets: standalone documents, each from its own model call."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures import sample
from drama_agent.fixtures.sample import SAMPLE_SEED, sample_request
from drama_agent.jobqueue import InMemoryJobQueue
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Job, ProductionPackage
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.pipeline.steps import PipelineState, completed_in_order
from drama_agent.render import render_markdown
from drama_agent.store import InMemoryRepository
from drama_agent.validation import PackageIndex, check_locks
from drama_agent.validation import hard_checks as hc
from drama_agent.worker import JobWorker, pause_job, resume_job

from .conftest import run_pipeline, wait_job

SHEETS = ("cast", "sets", "props")
BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}


def stages(job: Job) -> dict[str, str]:
    return {name: s.status for name, s in job.stages.items()}


# ---------------------------------------------------------------------------
# Pipeline


def test_full_run_produces_three_standalone_sheets(package: ProductionPackage):
    assert package.cast_sheet and package.set_sheet and package.prop_sheet
    # Names are program-owned: copied from the story bible, not trusted from the model.
    assert {c.name for c in package.cast_sheet.characters} == {
        c.name for c in package.story_bible.characters
    }
    assert package.set_sheet.locations[0].name == "林晓的旧书店"
    # Asset requirements now draw their design text and must_keep from the sheets.
    reqs = package.asset_requirements.by_id()
    scar = reqs["ar_character_identity_ch_zhou"]
    assert "右手背一块旧烫伤疤" in scar.design_brief
    assert "右手背烫伤疤的位置与形状" in scar.must_keep
    day = reqs["ar_location_loc_shop_ls_shop_day"]  # (the night one has a storyboard override)
    assert "门口风铃" in day.must_keep and "书架上书籍的具体摆放" in day.design_freedom


def test_sheet_steps_see_the_script_but_not_each_other_and_feed_the_storyboard():
    client = FixtureModelClient()
    run_pipeline(client)
    ctx = {c.stage: c.context for c in client.calls}
    for stage in SHEETS:
        assert "episodes" in ctx[stage] and "story_bible" in ctx[stage]
        assert not {"cast_sheet", "set_sheet", "prop_sheet"} & set(ctx[stage])
    assert {"cast_sheet", "set_sheet", "prop_sheet"} <= set(ctx["storyboard"])


class InFlight(FixtureModelClient):
    """Records how many model calls overlap."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.now = self.peak = 0

    async def generate(self, request, output_model):
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(0.02)
            return await super().generate(request, output_model)
        finally:
            self.now -= 1


def test_sheet_calls_run_concurrently():
    client = InFlight()
    job, _ = run_pipeline(client)
    assert job.status == "completed"
    assert client.peak == 3  # the three sheets; every other step runs alone


def test_a_failing_sheet_retries_alone():
    def drop_a_character(output, attempt):
        if attempt == 0:
            output.cast_sheet.characters.pop()
        return output

    client = FixtureModelClient(faults={"cast": drop_a_character})
    job, repo = run_pipeline(client)
    assert job.status == "completed"
    assert [job.stages[s].attempts for s in SHEETS] == [2, 1, 1]
    gate = next(e for e in repo.list_events(job.job_id) if e.kind == "gate_issues")
    assert gate.stage == "cast"
    assert gate.data["issues"] == ["design_sheet ch_mom: cast_sheet: character has no entry"]


def test_a_sheet_that_never_passes_ends_needs_revision_with_the_others_kept():
    def wrong_parent(output, attempt):
        output.set_sheet.states[0].location_id = "loc_room"
        return output

    job, repo = run_pipeline(FixtureModelClient(faults={"sets": wrong_parent}))
    assert job.status == "needs_revision"
    assert stages(job)["sets"] == "failed" and stages(job)["cast"] == "passed"
    assert stages(job)["storyboard"] == "pending"
    draft = repo.get_package(job.package_id, job.revision_id)
    assert draft.cast_sheet is not None  # the sheets that passed are in the draft
    [issue] = {i.evidence for i in draft.qa_report.hard_issues}
    assert "belongs to loc_shop, not loc_room" in issue


def test_an_exception_in_one_sheet_keeps_the_others_and_fails_the_job():
    def boom(output, attempt):
        raise ConnectionError("gateway down")

    repo, queue = InMemoryRepository(), InMemoryJobQueue()
    repo.save_job(Job(job_id="job_1", request=sample_request()))
    client = FixtureModelClient(faults={"cast": boom}, stream_delay_s=0.05)
    job = asyncio.run(Orchestrator(repo, client, checkpoints=queue).run_job("job_1"))
    assert job.status == "failed" and "gateway down" in job.failure_reason
    assert stages(job)["cast"] == "failed" and "gateway down" in job.stages["cast"].error
    # The siblings finished and were checkpointed: a retry won't pay for them again.
    assert stages(job)["sets"] == stages(job)["props"] == "passed"
    state, completed = PipelineState.from_checkpoint(job.request, queue.load_checkpoint("job_1"))
    assert {"sets", "props"} <= set(completed) and state.set_sheet and state.prop_sheet
    assert all(c.status != "running" for c in repo.list_llm_calls("job_1"))
    failed = [e for e in repo.list_events("job_1") if e.kind == "job_failed"]
    assert failed[0].stage == "cast"


def test_a_cancelled_job_leaves_no_call_running():
    repo = InMemoryRepository()
    repo.save_job(Job(job_id="job_1", request=sample_request()))
    orch = Orchestrator(repo, FixtureModelClient(stream_delay_s=0.5))

    async def run_then_cancel():
        task = asyncio.create_task(orch.run_job("job_1"))
        await asyncio.sleep(0.1)  # mid-way through the ingest call's stream
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_then_cancel())
    [call] = repo.list_llm_calls("job_1")
    assert call.status == "failed" and call.error == "cancelled before the response finished"


def test_pause_during_the_sheets_keeps_the_finished_one():
    repo, queue = InMemoryRepository(), InMemoryJobQueue()
    repo.save_job(Job(job_id="job_1", request=sample_request()))
    queue.enqueue("job_1")

    def press_pause(output, attempt):
        pause_job(repo, queue, "job_1")
        return output

    client = FixtureModelClient(faults={"cast": press_pause})
    worker = JobWorker(repo, queue, Orchestrator(repo, client, checkpoints=queue), retry_base_s=0)
    asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    assert job.status == "paused"
    assert stages(job)["cast"] == "passed"
    assert stages(job)["sets"] == stages(job)["props"] == "pending"

    client.faults.clear()
    resume_job(repo, queue, "job_1")
    asyncio.run(worker.run_once())
    job = repo.get_job("job_1")
    assert job.status == "completed"
    assert [c.stage for c in client.calls].count("cast") == 1  # not re-run


# ---------------------------------------------------------------------------
# Old jobs, checkpoints and packages


def test_jobs_stored_before_the_sheets_get_the_new_steps():
    old = {s: {"status": "passed"} for s in ("ingest", "outline", "script", "storyboard", "review")}
    done = Job.model_validate(
        {"job_id": "j", "request": BODY, "status": "completed", "stages": old}
    )
    assert list(done.stages) == [
        "ingest", "outline", "script", "cast", "sets", "props", "storyboard", "review",
    ]  # fmt: skip
    assert stages(done)["cast"] == "skipped"
    paused = Job.model_validate({"job_id": "j", "request": BODY, "status": "paused", "stages": old})
    # Matches what will run: the sheets, then storyboard and review again.
    assert stages(paused) == {
        "ingest": "passed", "outline": "passed", "script": "passed", "cast": "pending",
        "sets": "pending", "props": "pending", "storyboard": "pending", "review": "pending",
    }  # fmt: skip


def test_an_old_checkpoint_reruns_storyboard_after_the_new_sheets():
    assert completed_in_order(["ingest", "outline", "script", "storyboard"]) == [
        "ingest", "outline", "script",
    ]  # fmt: skip
    assert completed_in_order(["props", "ingest", "script", "outline", "cast"]) == [
        "ingest", "outline", "script", "cast", "props",
    ]  # fmt: skip

    repo, queue = InMemoryRepository(), InMemoryJobQueue()
    req = sample_request()
    state = PipelineState(request=req, package_id="pkg_x", revision_id="rev_x")
    state.source_brief = sample.sample_source_brief(req)
    state.story_bible = sample.sample_story_bible()
    state.episodes = sample.sample_episodes()
    queue.save_checkpoint(
        "job_1", state.to_checkpoint(["ingest", "outline", "script", "storyboard"])
    )
    repo.save_job(Job(job_id="job_1", request=req))
    client = FixtureModelClient()
    job = asyncio.run(Orchestrator(repo, client, checkpoints=queue).run_job("job_1"))
    assert job.status == "completed"
    assert [c.stage for c in client.calls][-2:] == ["storyboard", "review"]
    assert {c.stage for c in client.calls} == {*SHEETS, "storyboard", "review"}


def test_old_packages_without_sheets_still_load_and_pass_checks(package):
    raw = package.model_dump(mode="json")
    for key in ("cast_sheet", "set_sheet", "prop_sheet"):
        raw.pop(key)
    lin = raw["story_bible"]["characters"][0]
    lin["appearance"], lin["costume"] = "28岁女性，短发", "深色毛衣配店铺围裙"
    raw["story_bible"]["locations"][0]["layout"] = "狭长店面，玻璃门正对柜台"
    old = ProductionPackage.model_validate(raw)
    assert old.cast_sheet is None
    # The old visual text is kept (not dropped) and survives a save/load round trip.
    kept = old.story_bible.characters[0].legacy
    assert kept == {"appearance": "28岁女性，短发", "costume": "深色毛衣配店铺围裙"}
    again = ProductionPackage.model_validate(old.model_dump(mode="json"))
    assert again.story_bible.characters[0].legacy == kept
    # Asset briefs fall back to it when there's no sheet.
    from drama_agent.pipeline.asset_aggregation import aggregate_asset_requirements

    reqs = aggregate_asset_requirements(
        old.story_bible, [s.model_copy() for s in old.shot_list], old.asset_requirements.style_brief
    ).by_id()
    assert "28岁女性，短发" in reqs["ar_character_identity_ch_lin"].design_brief
    assert "玻璃门正对柜台" in reqs["ar_location_loc_shop_ls_shop_day"].design_brief
    idx = PackageIndex.build(old)
    opts = sample_request().options
    assert hc.check_cast_sheet(old, idx, opts) == hc.check_set_sheet(old, idx, opts) == []
    assert "Cast sheet" not in render_markdown(old)


# ---------------------------------------------------------------------------
# Checks


def test_coverage_checks_report_missing_unknown_duplicate_and_misparented(package):
    pkg = package.model_copy(deep=True)
    cast = pkg.cast_sheet
    cast.characters.append(cast.characters[0].model_copy(update={"character_id": "ch_ghost"}))
    cast.characters.append(cast.characters[1].model_copy())  # duplicate ch_zhou
    cast.looks = [lk for lk in cast.looks if lk.look_id != "lk_mei_work"]
    cast.looks[0].character_id = "ch_zhou"  # lk_lin_apron under the wrong character
    found = {
        (i.object_id, i.evidence)
        for i in hc.check_cast_sheet(pkg, PackageIndex.build(pkg), sample_request().options)
    }
    assert ("ch_ghost", "cast_sheet: character is not in the story bible") in found
    assert ("ch_zhou", "cast_sheet: character described twice") in found
    assert ("lk_mei_work", "cast_sheet: look has no entry") in found
    assert (
        "lk_lin_apron",
        "cast_sheet: look belongs to ch_lin, not ch_zhou (character_id)",
    ) in found


def test_off_screen_character_must_not_appear_in_a_shot(package):
    pkg = package.model_copy(deep=True)
    lin = next(c for c in pkg.cast_sheet.characters if c.character_id == "ch_lin")
    lin.on_screen = False
    issues = hc.check_cast_sheet(pkg, PackageIndex.build(pkg), sample_request().options)
    assert issues and all("on_screen=false" in i.evidence for i in issues)


def test_prose_leaks_in_sheets_are_hard_issues(package):
    pkg = package.model_copy(deep=True)
    pkg.prop_sheet.props[0].handling = "收在 prop_photo 旁边"
    issues = hc.check_prose(pkg, PackageIndex.build(pkg), sample_request().options)
    assert any(i.object_id == "prop_photo" for i in issues)


def test_a_lock_covers_the_design_sheet_entry(package):
    base = package.model_copy(deep=True)
    base.locked_ids = ["ch_zhou"]
    new = base.model_copy(deep=True)
    zhou = next(c for c in new.cast_sheet.characters if c.character_id == "ch_zhou")
    zhou.hair = "染黑的短发"
    [issue] = check_locks(base, new)
    assert issue.object_id == "ch_zhou" and "design sheet" in issue.evidence


# ---------------------------------------------------------------------------
# API


def test_sheet_and_schema_endpoints():
    with TestClient(create_app(poll_s=0.01)) as http:
        job_id = http.post("/v1/drama-jobs", json=BODY).json()["job_id"]
        job = wait_job(http, job_id)
        base = f"/v1/drama-packages/{job['package_id']}"
        cast = http.get(f"{base}/cast-sheet").json()
        assert cast["schema_version"] == "1.0" and cast["characters"][0]["character_id"]
        assert http.get(f"{base}/set-sheet").json()["locations"]
        assert http.get(f"{base}/prop-sheet").json()["props"]
        assert http.get(f"{base}/nope-sheet").status_code == 422
        assert "## Cast sheet" in http.get(base, params={"format": "markdown"}).text

        schema = http.get("/v1/schemas/cast-sheet.json").json()
        assert schema["$id"] == "/v1/schemas/cast-sheet.json"
        assert schema["title"] == "CastSheet" and "characters" in schema["required"]
        for name in ("set-sheet", "prop-sheet"):
            assert http.get(f"/v1/schemas/{name}.json").status_code == 200
        assert http.get("/v1/schemas/story-bible.json").status_code == 404


@pytest.mark.parametrize("sheet", ["cast-sheet", "set-sheet", "prop-sheet"])
def test_sheet_endpoint_explains_old_packages(sheet, package):
    repo = InMemoryRepository()
    old = package.model_copy(update={"cast_sheet": None, "set_sheet": None, "prop_sheet": None})
    repo.save_package(old, published=True)
    with TestClient(create_app(repo=repo, workers=0)) as http:
        r = http.get(f"/v1/drama-packages/{old.package_id}/{sheet}")
        assert r.status_code == 404 and "made before" in r.json()["detail"]


def test_legacy_fields_are_hidden_from_the_model_and_absent_from_new_packages(package):
    import json

    from drama_agent.pipeline.steps import OutlineOutput

    assert "legacy" not in json.dumps(OutlineOutput.model_json_schema())
    assert '"legacy"' not in package.model_dump_json()


def test_asset_briefs_carry_what_designers_need(package):
    reqs = package.asset_requirements.by_id()
    day = reqs["ar_location_loc_shop_ls_shop_day"].design_brief
    assert "唯一入口为临街玻璃门" in day  # entrances, for the top-down blocking view
    assert "白天自然光从门口进入" in day  # base lighting
    look = reqs["ar_character_look_ch_zhou_lk_zhou_rain"]
    assert "右手背一块旧烫伤疤" in look.design_brief  # same person: identifying traits
    assert "右手背烫伤疤的位置与形状" in look.must_keep and "鸭舌帽遮住半张脸" in look.must_keep


def test_a_variant_without_a_sheet_entry_keeps_its_own_description(package):
    from drama_agent.pipeline.asset_aggregation import aggregate_asset_requirements
    from drama_agent.pipeline.steps import DesignSheets

    sets = package.set_sheet.model_copy(deep=True)
    sets.states = [st for st in sets.states if st.location_state_id != "ls_shop_night"]
    reqs = aggregate_asset_requirements(
        package.story_bible,
        [s.model_copy() for s in package.shot_list],
        package.asset_requirements.style_brief,
        sheets=DesignSheets(cast=package.cast_sheet, sets=sets, props=package.prop_sheet),
    ).by_id()
    assert "雨夜打烊" in reqs["ar_location_loc_shop_ls_shop_night"].design_brief


def test_recorder_finish_is_idempotent():
    from drama_agent.tracing import Tracer

    repo = InMemoryRepository()
    rec = Tracer(repo, "j").recorder("cast", 0)
    rec.start(model="m", api="fixture", request={})
    rec.finish(usage={"prompt_tokens": 1})
    rec.finish(error="cancelled before the response finished")  # late cancel: ignored
    [call] = repo.list_llm_calls("j")
    assert call.status == "succeeded" and call.error is None
