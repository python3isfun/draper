"""Hand edits to cast, set and prop entries, in place."""

import time

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import FixtureImageClient
from drama_agent.images import design_slots
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.sheet_edit import EditError, EntryLocked, edit_entry
from drama_agent.store import InMemoryRepository

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
LAYOUT = "A narrow galley shop: counter on the left, workbench at the back under one window."


def reqs(pkg, entity_id):
    return {
        r.requirement_id: r
        for r in pkg.asset_requirements.items
        if r.asset_type != "shot_reference" and r.subject_refs[0].entity_id == entity_id
    }


def test_editing_a_sets_areas_changes_its_pictures(package):
    """A set's areas are its mid-shot pictures: edit the list and each state's deliverables
    follow, the establishing shot and the floor plan first."""
    assert {len(r.deliverables) for r in reqs(package, "loc_shop").values()} == {8}
    edit = edit_entry(package, "loc_shop", sheet={"areas": [
        {"name": "Behind the counter", "view": "From the customer side, facing the back wall"},
        {"name": "Front door", "view": "From the aisle, facing the glass front door"},
    ]})  # fmt: skip
    assert edit.changed == ["sheet.areas"]
    for r in reqs(package, "loc_shop").values():
        assert [d.view for d in r.deliverables] == [
            "wide", "top-down", "mid-shot: Behind the counter", "mid-shot: Front door"]  # fmt: skip
        assert r.deliverables[2].deliverable_id == f"d_{r.requirement_id}_area_1"
    # Areas name pictures of the set; no keyframe is written against them, so listing or
    # changing them leaves every shot's design print (and so its keyframes) as it was.
    from drama_agent.shots.staleness import design_print

    shot = next(s for s in package.shot_list if s.location_id == "loc_shop")
    had = design_print(package, shot)
    edit_entry(package, "loc_shop", sheet={"areas": []})
    assert design_print(package, shot) == had
    edit_entry(package, "loc_shop", sheet={"layout": "Another room entirely"})
    assert design_print(package, shot) != had
    with pytest.raises(EditError):  # six at most
        edit_entry(package, "loc_shop", sheet={"areas": [{"name": str(i), "view": "v"}
                                                         for i in range(7)]})  # fmt: skip


def test_editing_a_location_rebuilds_the_design_text_of_its_states(package):
    edit = edit_entry(package, "loc_shop", sheet={"layout": f"  {LAYOUT}\n"})
    assert edit.changed == ["sheet.layout"]
    loc = next(x for x in package.set_sheet.locations if x.location_id == "loc_shop")
    assert loc.layout == LAYOUT  # trimmed
    shop = reqs(package, "loc_shop")
    assert set(edit.requirement_ids) == set(shop) and len(shop) == 2
    assert all(LAYOUT in r.design_brief for r in shop.values())
    assert not any(LAYOUT in r.design_brief for r in reqs(package, "loc_room").values())
    log = package.change_log[-1]
    assert log.target_ids == ["loc_shop"] and "sheet.layout" in log.note


def test_must_keep_lines_and_a_state_description(package):
    keep = ["A brass bell over the door.", "", "  Rain on the window.  "]
    edit = edit_entry(
        package, "ls_shop_night", sheet={"must_keep": keep},
        bible={"description": "The shop after closing, one lamp on."},
    )  # fmt: skip
    assert edit.changed == ["sheet.must_keep", "bible.description"]
    assert edit.requirement_ids == ["ar_location_loc_shop_ls_shop_night"]
    req = reqs(package, "loc_shop")["ar_location_loc_shop_ls_shop_night"]
    assert req.must_keep[-2:] == ["A brass bell over the door.", "Rain on the window."]
    assert "The shop after closing, one lamp on." in req.design_brief
    # The design board shows the new one-liner.
    slot = next(s for s in design_slots(package) if s["requirement_id"] == req.requirement_id)
    assert slot["variant"] == "The shop after closing, one lamp on."


def test_a_character_edit_reaches_its_looks(package):
    edit = edit_entry(package, "ch_frank", sheet={"distinguishing_features": "A scar on his chin."})
    frank = reqs(package, "ch_frank")
    assert set(edit.requirement_ids) == set(frank)
    assert any(r.asset_type == "character_look" for r in frank.values())
    assert all("A scar on his chin." in r.design_brief for r in frank.values())


def test_no_change_logs_nothing(package):
    loc = next(x for x in package.set_sheet.locations if x.location_id == "loc_shop")
    before = len(package.change_log)
    edit = edit_entry(package, "loc_shop", sheet={"layout": loc.layout})
    assert edit.changed == [] and edit.requirement_ids == []
    assert len(package.change_log) == before


@pytest.mark.parametrize(
    ("entry", "sheet", "bible", "match"),
    [
        ("loc_shop", {"location_id": "loc_x"}, {}, "can't be edited"),
        ("loc_shop", {"name": "Shop"}, {}, "can't be edited"),
        ("loc_shop", {"colour": "red"}, {}, "can't be edited"),
        ("loc_shop", {"interior_exterior": "OUTSIDE"}, {}, "interior_exterior"),
        ("loc_shop", {"must_keep": "one string"}, {}, "must_keep"),
        ("loc_shop", {}, {"name": "Shop"}, "no one-line"),
        ("loc_shop", {}, {"summary": "A shop."}, "no one-line"),  # story text, not design
        ("lk_frank_rain", {}, {"applies_to_scene_ids": []}, "can't be edited"),
        ("ch_frank", {}, {"description": "x"}, "no one-line"),
    ],
)
def test_bad_edits_are_refused(package, entry, sheet, bible, match):
    before = package.model_dump_json()
    with pytest.raises(EditError, match=match):
        edit_entry(package, entry, sheet=sheet, bible=bible)
    assert package.model_dump_json() == before


@pytest.mark.parametrize(
    ("entry", "locked"),
    [
        ("loc_shop", "loc_shop"),
        ("ls_shop_night", "loc_shop"),  # its parent
        ("lk_frank_rain", "ch_frank"),
        ("ch_frank", "ar_character_look_ch_frank_lk_frank_rain"),  # a requirement built from it
    ],
)
def test_locked_entries_are_refused(package, entry, locked):
    package.locked_ids = [locked]
    before = package.model_dump_json()
    with pytest.raises(EntryLocked, match=locked):
        edit_entry(package, entry, sheet={"must_keep": ["x"]})
    assert package.model_dump_json() == before


def test_replacing_model_written_design_text_is_reported(package):
    night = reqs(package, "loc_shop")["ar_location_loc_shop_ls_shop_night"]
    written = night.design_brief
    edit = edit_entry(package, "loc_shop", sheet={"layout": LAYOUT})
    assert edit.replaced_written == ["ar_location_loc_shop_ls_shop_night"]
    assert night.design_brief != written and LAYOUT in night.design_brief
    # Once rebuilt, the text is the sheets' own: a second edit replaces nothing written.
    assert edit_entry(package, "loc_shop", sheet={"layout": "Smaller."}).replaced_written == []


def test_unknown_entry(package):
    with pytest.raises(KeyError):
        edit_entry(package, "loc_nope", sheet={"layout": "x"})


def test_replace_package_overwrites_one_revision(package):
    repo = InMemoryRepository()
    with pytest.raises(KeyError):
        repo.replace_package(package)
    repo.save_package(package, published=True)
    edit_entry(package, "loc_shop", sheet={"layout": LAYOUT})
    repo.replace_package(package)
    stored = repo.get_package(package.package_id, package.revision_id)
    assert stored.set_sheet.locations[0].layout == LAYOUT


def test_edit_endpoint(tmp_path):
    app = create_app(
        client=FixtureModelClient(), image_client=FixtureImageClient(),
        media=MediaStore(tmp_path), poll_s=0.01,
    )  # fmt: skip
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        pid, rid = job["package_id"], job["revision_id"]
        url = f"/v1/drama-packages/{pid}/entries/loc_shop"
        # An image drawn before the edit is reported.
        http.post(f"/v1/drama-packages/{pid}/images?draft=true")
        deadline = time.monotonic() + 10
        while http.get(f"/v1/drama-packages/{pid}/images").json()["queued"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        r = http.patch(url, json={"revision_id": rid, "sheet": {"layout": LAYOUT}})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["changed"] == ["sheet.layout"] and len(out["requirement_ids"]) == 2
        assert out["replaced_written"] == ["ar_location_loc_shop_ls_shop_night"]
        assert out["drawn_before"] and all(
            "wide" in t or "top-down" in t or "mid-shot" in t for t in out["drawn_before"]
        )
        pkg = http.get(f"/v1/drama-packages/{pid}", params={"revision_id": rid}).json()
        assert pkg["revision_id"] == rid  # in place: no new revision
        assert pkg["set_sheet"]["locations"][0]["layout"] == LAYOUT
        # Errors.
        bad = {"revision_id": rid, "sheet": {"location_id": "x"}}
        assert http.patch(url, json=bad).status_code == 422
        assert http.patch(url, json={"revision_id": "rev_nope"}).status_code == 404
        tell = {"revision_id": rid, "bible": {"summary": "A shop."}}
        assert http.patch(url, json=tell).status_code == 422
        nope = f"/v1/drama-packages/{pid}/entries/loc_nope"
        assert http.patch(nope, json={"revision_id": rid}).status_code == 404
