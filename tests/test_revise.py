"""The revise run (pipeline/revise.py): scoped re-runs, restored scope and locks, publishing,
and carrying the story's work forward to the new revision."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Revision, RevisionRequest
from drama_agent.models.package import utcnow
from drama_agent.pipeline.impact import analyze_impact
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.pipeline.revise import ReviseRunner, busy_work
from drama_agent.shot_frames import PLAN_SHOT_REVISED, ShotPlan, design_print, plan_outdated
from drama_agent.store.sqlite import SqliteRepository
from drama_agent.validation import PackageIndex

from .conftest import run_pipeline, wait_job


class Reviser(FixtureModelClient):
    """The fixture client, editing its output in revise runs only (it returns the sample
    story, which is also the base revision)."""

    def __init__(self, edit) -> None:
        super().__init__()
        self.edit = edit
        self.stages: list[str] = []

    async def generate(self, request, output_model):
        out = await super().generate(request, output_model)
        if "revision" in request.context:
            self.stages.append(request.stage)
            self.edit(request.stage, out)
        return out


def _revise(edit, req: RevisionRequest, *, setup=None, wait_s: float = 0.0):
    job, repo = run_pipeline()
    base = repo.get_package(job.package_id)
    if setup:
        setup(repo, base)
    impact = analyze_impact(base, req.target_ids, req.locked_ids, req.visual_change)
    rev = Revision(
        revision_id="rev_new", package_id=base.package_id, base_revision_id=base.revision_id,
        request=req, impacted_ids=impact.impacted_ids,
        stale_requirement_ids=impact.stale_requirement_ids,
    )  # fmt: skip
    repo.save_revision(rev)
    client = Reviser(edit)
    runner = ReviseRunner(repo, Orchestrator(repo, client), wait_s=wait_s, poll_s=0.01)
    done = asyncio.run(runner.run(rev.revision_id))
    return done, repo, base, client


def _req(targets, instruction="x", locked=(), visual=False, base="") -> RevisionRequest:
    return RevisionRequest(base_revision_id=base or "b", target_ids=list(targets),
                           instruction=instruction, locked_ids=list(locked),
                           visual_change=visual)  # fmt: skip


def _look(sheet, look_id):
    return next(lk for lk in sheet.looks if lk.look_id == look_id)


def test_a_look_revision_reruns_only_the_cast_sheet_and_puts_back_the_rest():
    def edit(stage, out):
        if stage == "cast":
            _look(out.cast_sheet, "lk_frank_rain").wardrobe = "A yellow fisherman's raincoat."
            # Outside the scope: put back by the program.
            _look(out.cast_sheet, "lk_maya_apron").wardrobe = "A ball gown."

    rev, repo, base, client = _revise(edit, _req(["lk_frank_rain"]))
    assert rev.status == "published", rev.error
    assert client.stages == ["cast", "review"] and rev.steps == ["cast", "review"]
    new = repo.get_package(base.package_id)
    assert new.revision_id == "rev_new"
    assert _look(new.cast_sheet, "lk_frank_rain").wardrobe == "A yellow fisherman's raincoat."
    assert _look(new.cast_sheet, "lk_maya_apron") == _look(base.cast_sheet, "lk_maya_apron")
    assert "lk_frank_rain" in rev.changed_ids and "lk_maya_apron" not in rev.changed_ids
    assert new.episodes == base.episodes and new.shot_list == base.shot_list
    # The look's design brief is written again from the new sheet; the others' stay.
    reqs = {r.requirement_id: r for r in new.asset_requirements.items}
    for r in base.asset_requirements.items:
        if not any("lk_frank_rain" in (s.variant_id, s.entity_id) for s in r.subject_refs):
            assert reqs[r.requirement_id].design_brief == r.design_brief
    entry = new.change_log[-1]
    assert entry.revision_id == "rev_new" and entry.note.startswith("Revised")
    assert repo.get_job("job_test").revision_id == "rev_new"


def test_a_scene_revision_keeps_locked_lines_and_scenes_outside_its_scope():
    def edit(stage, out):
        if stage == "script":
            for ep in out.episodes:
                for sc in ep.scenes:
                    for b in sc.beats:
                        if b.dialogue and b.dialogue.dialogue_id in (
                            "dl_02_01_01",
                            "dl_02_01_02",
                            "dl_01_01_01",
                        ):
                            b.dialogue.text += " Really."

    rev, repo, base, _ = _revise(edit, _req(["sc_02_01"], locked=["dl_02_01_01"]))
    assert rev.status == "published", rev.error
    assert rev.steps == ["script", "storyboard", "review"]
    old, new = (
        PackageIndex.build(base).dialogue,
        PackageIndex.build(repo.get_package(base.package_id)).dialogue,
    )
    assert new["dl_02_01_01"] == old["dl_02_01_01"]  # locked
    assert new["dl_01_01_01"] == old["dl_01_01_01"]  # an earlier scene: outside the scope
    assert new["dl_02_01_02"].text == old["dl_02_01_02"].text + " Really."
    assert "dl_02_01_02" in rev.changed_ids and "dl_01_01_01" not in rev.changed_ids
    # The request's lock held for this run only.
    assert repo.get_package(base.package_id).locked_ids == base.locked_ids


def _plan(base, shot_id, **kw) -> ShotPlan:
    shot = next(s for s in base.shot_list if s.shot_id == shot_id)
    return ShotPlan(package_id=base.package_id, revision_id=base.revision_id, shot_id=shot_id,
                    source="model", design_print=design_print(base, shot),
                    updated_at=utcnow() - timedelta(minutes=5), **kw)  # fmt: skip


def test_a_shot_revision_carries_plans_forward_and_flags_the_revised_shots_plan():
    def edit(stage, out):
        if stage == "storyboard":
            for s in out.shots:
                if s.shot_id in ("sh_01_01_02", "sh_03_01_01"):
                    s.visual_action += " She hesitates at the door."

    def setup(repo, base):
        repo.save_shot_plan(_plan(base, "sh_01_01_02"))
        repo.save_shot_plan(_plan(base, "sh_01_02_01"))

    rev, repo, base, _ = _revise(edit, _req(["sh_01_01_02"]), setup=setup)
    assert rev.status == "published", rev.error
    assert rev.moved == 2 and repo.list_shot_plans(base.package_id, base.revision_id) == []
    new = repo.get_package(base.package_id)
    shots = {s.shot_id: s for s in new.shot_list}
    assert shots["sh_03_01_01"] == next(s for s in base.shot_list if s.shot_id == "sh_03_01_01")
    assert rev.changed_ids == ["sh_01_01_02"]
    plans = {p.shot_id: p for p in repo.list_shot_plans(new.package_id, new.revision_id)}
    assert plan_outdated(new, shots["sh_01_01_02"], plans["sh_01_01_02"]) == PLAN_SHOT_REVISED
    assert plan_outdated(new, shots["sh_01_02_01"], plans["sh_01_02_01"]) is None


def test_publishing_waits_for_work_in_flight_and_fails_if_it_never_ends():
    def setup(repo, base):
        repo.save_shot_plan(_plan(base, "sh_01_01_02", status="writing"))

    rev, repo, base, _ = _revise(lambda *_: None, _req(["sh_01_01_02"]), setup=setup)
    assert rev.status == "failed" and "cut plans are being written" in rev.error
    assert repo.get_package(base.package_id).revision_id == base.revision_id
    assert busy_work(repo, base.package_id, base.revision_id)


def test_issues_the_gate_cannot_fix_leave_the_story_on_its_base_revision():
    def edit(stage, out):
        if stage == "storyboard":
            for s in out.shots:
                if s.shot_id == "sh_01_01_02":
                    s.duration_ms += 5000  # breaks the episode's timeline total

    rev, repo, base, client = _revise(edit, _req(["sh_01_01_02"]))
    assert rev.status == "needs_revision" and rev.issues
    assert client.stages.count("storyboard") == 3  # the first attempt and two repairs
    assert repo.get_package(base.package_id).revision_id == base.revision_id
    assert repo.get_package(base.package_id, "rev_new") is not None  # the draft, kept


def test_interrupted_revisions_are_marked_failed_on_restart():
    _, repo = run_pipeline()
    repo.save_revision(Revision(revision_id="rev_x", package_id="p", base_revision_id="b",
                                request=_req(["sc_01_01"]), status="running"))  # fmt: skip
    assert ReviseRunner(repo, None).recover() == 1
    assert repo.get_revision("rev_x").status == "failed"


def test_sqlite_carries_rows_forward_with_their_data(tmp_path):
    repo = SqliteRepository(tmp_path / "x.db")
    _, mem = run_pipeline()
    base = mem.get_package(mem.get_job("job_test").package_id)
    repo.save_shot_plan(_plan(base, "sh_01_01_02"))
    assert repo.carry_forward(base.package_id, base.revision_id, "rev_2") == 1
    plan = repo.get_shot_plan(base.package_id, "rev_2", "sh_01_01_02")
    assert plan is not None and plan.revision_id == "rev_2"
    assert repo.get_shot_plan(base.package_id, base.revision_id, "sh_01_01_02") is None


def test_the_api_runs_a_revision_and_the_story_moves_to_it():
    with TestClient(create_app(poll_s=0.01)) as client:
        body = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
        job = wait_job(client, client.post("/v1/drama-jobs", json=body).json()["job_id"])
        pid, base_rev = job["package_id"], job["revision_id"]
        rev = client.post(f"/v1/drama-packages/{pid}/revisions", json={
            "base_revision_id": base_rev, "target_ids": ["lk_frank_rain"], "instruction": "x",
        }).json()  # fmt: skip
        for _ in range(500):
            got = client.get(f"/v1/revisions/{rev['revision_id']}").json()
            if got["status"] not in ("queued", "running"):
                break
            asyncio.run(asyncio.sleep(0.01))
        assert got["status"] == "published", got
        assert client.get(f"/v1/drama-packages/{pid}").json()["revision_id"] == rev["revision_id"]
        assert (
            client.get(f"/v1/drama-jobs/{job['job_id']}").json()["revision_id"]
            == rev["revision_id"]
        )
        listed = client.get("/v1/revisions", params={"package_id": pid}).json()
        assert listed[0]["revision_id"] == rev["revision_id"]
        # A request based on the old revision is now stale.
        again = client.post(f"/v1/drama-packages/{pid}/revisions", json={
            "base_revision_id": base_rev, "target_ids": ["lk_frank_rain"], "instruction": "x",
        })  # fmt: skip
        assert again.status_code == 409


def test_the_storys_pictures_and_keyframes_follow_and_only_what_changed_is_out_of_date(
    tmp_path,
):
    from .test_redesign import by_image, picks
    from .test_shot_frames import Board

    target: dict = {}

    def edit(stage, out):
        if stage == "sets":
            st = next(s for s in out.set_sheet.states
                      if s.location_state_id == target["state"])  # fmt: skip
            st.lighting = "Cold green fluorescent tubes, one of them flickering."

    with Board(tmp_path, text=Reviser(edit)) as b:
        b.design()
        before = picks(b)
        assert before["outdated"] == {}
        sid = b.board()["shots"][0]["shot_id"]
        b.http.post(f"{b.base}/{sid}", json={"draws": 1})
        [take] = b.wait() and b.takes(sid)
        b.http.post(f"/v1/shot-takes/{take['take_id']}/approve", json={"n": 0, "next": False})
        pkg = b.repo.get_package(b.pid)
        target["state"] = next(s.location_state_id for s in pkg.shot_list if s.shot_id == sid)

        rev = b.http.post(f"/v1/drama-packages/{b.pid}/revisions", json={
            "base_revision_id": pkg.revision_id, "target_ids": [target["state"]],
            "instruction": "Harsher light",
        }).json()  # fmt: skip
        deadline = time.monotonic() + 30
        while (got := b.http.get(f"/v1/revisions/{rev['revision_id']}").json())["status"] in (
            "queued", "running",
        ):  # fmt: skip
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert got["status"] == "published", got
        assert target["state"] in got["changed_ids"]

        after = picks(b)
        assert {i["image_id"] for i in after["images"]} == {i["image_id"] for i in before["images"]}
        imgs = by_image(after)
        assert after["outdated"], "the state's pictures are out of date"
        for image_id, why in after["outdated"].items():
            req = imgs[image_id]["requirement_id"]
            # Only the revised state's pictures, and what was drawn from them.
            assert target["state"] in req or any("picture" in w for w in why), (req, why)
        # The approved keyframe was drawn from the state's picture: out of date too.
        assert b.shot(sid)["sub_shots"][0]["outdated"]


def test_a_shot_revision_reaches_only_the_shot_and_its_neighbours():
    def edit(stage, out):
        if stage == "storyboard":
            for s in out.shots:
                if s.shot_id in ("sh_01_01_02", "sh_01_01_04"):  # 04 isn't a neighbour
                    s.visual_action += " She hesitates."
            for o in out.design_overrides:  # every brief rewritten: only in-scope ones count
                o.design_brief = "Rewritten."

    rev, repo, base, _ = _revise(edit, _req(["sh_01_01_02"]))
    assert rev.status == "published", rev.error
    assert "sh_01_01_04" not in rev.changed_ids and "sh_01_01_02" in rev.changed_ids
    reach = set(rev.stale_requirement_ids)
    new = {r.requirement_id: r for r in repo.get_package(base.package_id).asset_requirements.items}
    for r in base.asset_requirements.items:
        if r.requirement_id not in reach:
            assert new[r.requirement_id].design_brief == r.design_brief, r.requirement_id


def test_a_hand_edit_during_the_run_stops_the_publish():
    box: dict = {}

    def edit(stage, out):
        if stage == "review":  # a person saves a hand edit while the run is going
            repo, base = box["repo"], box["base"]
            edited = base.model_copy(deep=True)
            edited.story_bible.logline += " (edited)"
            repo.replace_package(edited)

    rev, repo, base, _ = _revise(
        edit, _req(["lk_frank_rain"]), setup=lambda r, b: box.update(repo=r, base=b)
    )
    assert rev.status == "failed" and "edited by hand" in rev.error
    assert repo.get_package(base.package_id).revision_id == base.revision_id


def test_only_a_published_story_can_be_revised():
    with TestClient(create_app(poll_s=0.01)) as client:
        body = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
        job = wait_job(client, client.post("/v1/drama-jobs", json=body).json()["job_id"])
        repo = client.app.state.repo
        draft = repo.get_revision(job["revision_id"])
        draft.status = "needs_revision"
        repo.save_revision(draft)
        r = client.post(f"/v1/drama-packages/{job['package_id']}/revisions", json={
            "base_revision_id": job["revision_id"], "target_ids": ["lk_frank_rain"],
            "instruction": "x",
        })  # fmt: skip
        assert r.status_code == 409 and "published" in r.text
