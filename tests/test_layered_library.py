"""Characters and Sets, layered: a new look or state added by hand, and a keyframe draw sent
to a look (or a set state) as a take."""

import pytest

from drama_agent.images import design_slots
from drama_agent.models import JobOptions
from drama_agent.sheet_edit import EditError, EntryLocked, add_variant
from drama_agent.validation.hard_checks import run_hard_checks

from .test_shot_frames import Board


def test_a_new_look_gets_its_line_sheet_and_picture_slot(package):
    before = len(package.asset_requirements.items)
    added = add_variant(package, "ch_frank", "Torn evening suit, after the fight",
                        {"wardrobe": "A black suit, one sleeve ripped", "condition": "  bruised "})  # fmt: skip
    assert added.entry_id == "lk_frank_torn_evening_suit"
    look = next(lk for lk in package.story_bible.looks if lk.look_id == added.entry_id)
    assert look.character_id == "ch_frank" and look.applies_to_scene_ids == []
    sheet = next(lk for lk in package.cast_sheet.looks if lk.look_id == added.entry_id)
    assert sheet.condition == "bruised" and sheet.changes_from_base.startswith("Torn evening")
    assert len(package.asset_requirements.items) == before + 1
    req = package.asset_requirements.items[-1]
    assert (
        req.requirement_id == added.requirement_id == "ar_character_look_ch_frank_" + added.entry_id
    )
    assert req.used_by == [] and req.depends_on == ["ar_character_identity_ch_frank"]
    assert "one sleeve ripped" in req.design_brief
    # On the design board it is drawn from the face, as any look.
    slot = next(s for s in design_slots(package) if s["requirement_id"] == req.requirement_id)
    assert slot["parent_key"][0] == "ar_character_identity_ch_frank"
    assert slot["variant"] == "Torn evening suit, after the fight"
    assert package.change_log[-1].changed_ids == [added.entry_id, req.requirement_id]
    # The package still passes the hard checks; the same words get a new id.
    assert not [i for i in run_hard_checks(package, JobOptions()) if added.entry_id in str(i)]
    again = add_variant(package, "ch_frank", "Torn evening suit, after the fight")
    assert again.entry_id == "lk_frank_torn_evening_suit_2"


def test_a_new_set_state_gets_a_wide_and_a_top_down_view(package):
    added = add_variant(
        package, "loc_shop", "Ночь", {"time_of_day": "night", "lighting": "one lamp"}
    )
    assert added.entry_id == "ls_shop_new"  # no ASCII words to name it by
    assert [d.split("_")[-1] for d in added.deliverable_ids] == ["wide", "layout"]
    st = next(s for s in package.set_sheet.states if s.location_state_id == added.entry_id)
    assert st.lighting == "one lamp"


@pytest.mark.parametrize(
    ("parent", "description", "sheet", "error"),
    [
        ("ch_nobody", "x", {}, KeyError),
        ("ch_frank", "   ", {}, EditError),
        ("ch_frank", "x", {"pockets": "two"}, EditError),
        ("ch_frank", "x", {"look_id": "lk_mine"}, None),  # ids are the program's
    ],
)
def test_bad_new_variants(package, parent, description, sheet, error):
    if error is None:
        assert add_variant(package, parent, description, sheet).entry_id != "lk_mine"
        return
    with pytest.raises(error):
        add_variant(package, parent, description, sheet)


def test_a_locked_character_gets_no_new_look(package):
    package.locked_ids.append("ch_frank")
    with pytest.raises(EntryLocked):
        add_variant(package, "ch_frank", "Raincoat")


def test_new_look_through_the_api_and_its_library_page(tmp_path):
    with Board(tmp_path) as b:
        pkg = b.repo.get_package(b.pid)
        cid = pkg.story_bible.characters[0].character_id
        url = f"/v1/drama-packages/{b.pid}/variants"
        r = b.http.post(url, json={"parent_id": cid, "description": "Wedding dress, rain-soaked",
                                   "sheet": {"hair_and_makeup": "hair down, mascara running"}})  # fmt: skip
        assert r.status_code == 201, r.text
        out = r.json()
        view = b.http.get(f"/v1/library/characters/{out['lib_asset_id']}").json()
        look = next(lk for lk in view["looks"] if lk["asset_id"] == out["part_asset_id"])
        assert (
            look["description"] == "Wedding dress, rain-soaked"
            and look["used_in"][0]["shots"] == []
        )
        # It is drawn like any look: a new take on its slot, once the face has a pick.
        b.design()
        face = next(i for i in b.repo.list_images(b.pid, pkg.revision_id)
                    if i.requirement_id == f"ar_character_identity_{cid}" and i.status == "done")  # fmt: skip
        b.http.post(f"/v1/generated-images/{face.image_id}/select")
        r = b.http.post(f"/v1/drama-packages/{b.pid}/images/takes",
                        json={"requirement_id": out["requirement_id"],
                              "deliverable_id": out["deliverable_ids"][0], "draws": 1})  # fmt: skip
        assert r.status_code == 202, r.text
        assert (
            b.http.post(url, json={"parent_id": "ch_nobody", "description": "x"}).status_code == 404
        )
        assert b.http.post(url, json={"parent_id": cid, "description": ""}).status_code == 422


def _kept_keyframe(b) -> tuple[dict, dict]:
    """A finished keyframe take of a shot with people in it, and that shot."""
    b.design()
    shot = b.busiest()
    b.http.post(b.base, json={"shot_ids": [shot["shot_id"]], "draws": 1, "plan": False})
    b.wait()
    [take] = b.takes(shot["shot_id"])
    assert take["status"] == "done"
    return take, shot


def test_a_keyframe_draw_becomes_a_take_of_a_look(tmp_path):
    with Board(tmp_path) as b:
        take, shot = _kept_keyframe(b)
        base = f"/v1/shot-takes/{take['take_id']}/draws/0"
        targets = b.http.get(f"{base}/design-targets").json()
        kinds = [t["kind"] for t in targets]
        assert kinds.count("look") == len(shot["people"]) and kinds[-1] == "set"
        look = targets[0]
        assert look["sent_image_id"] is None and look["lib_asset_id"].startswith("chr_")
        assert look["part_asset_id"].startswith("look_") and "full body" in look["label"]
        before = {i.image_id for i in b.repo.list_images(b.pid, b.repo.get_package(b.pid).revision_id)
                  if i.selected}  # fmt: skip
        body = {"requirement_id": look["requirement_id"], "deliverable_id": look["deliverable_id"]}
        r = b.http.post(f"{base}/to-design", json=body)
        assert r.status_code == 201, r.text
        img = r.json()
        assert img["imported_from"] == f"keyframe {take['take_id']}:0 (shot {shot['shot_id']})"
        assert img["option_id"] is None and not img["selected"] and img["status"] == "done"
        assert b.media.path(img["file"]).exists()
        # Not the pick: the picks are as they were.
        rev = b.repo.get_package(b.pid).revision_id
        assert {i.image_id for i in b.repo.list_images(b.pid, rev) if i.selected} == before
        # Sent again: the same copy, now picked; the targets say so.
        again = b.http.post(f"{base}/to-design", json=body | {"pick": True}).json()
        assert again["image_id"] == img["image_id"] and again["selected"]
        sent = b.http.get(f"{base}/design-targets").json()[0]
        assert sent["sent_image_id"] == img["image_id"] and sent["picked"]
        # Its copy outlives the keyframe.
        b.http.delete(f"/v1/shot-takes/{take['take_id']}")
        assert b.http.get(f"/v1/generated-images/{img['image_id']}/content").status_code == 200
        # Only the shot's own looks and set state.
        other = next(r for r in b.repo.get_package(b.pid).asset_requirements.items
                     if r.asset_type == "character_identity")  # fmt: skip
        take2, _ = _kept_keyframe(b)
        r = b.http.post(f"/v1/shot-takes/{take2['take_id']}/draws/0/to-design",
                        json={"requirement_id": other.requirement_id,
                              "deliverable_id": other.deliverables[0].deliverable_id})  # fmt: skip
        assert r.status_code == 422
        assert b.http.get("/v1/shot-takes/nope/draws/0/design-targets").status_code == 404


def test_a_keyframe_is_developed_in_a_chat_and_comes_back_approved(tmp_path):
    with Board(tmp_path) as b:
        take, shot = _kept_keyframe(b)
        sid_ = shot["shot_id"]
        target = {
            "package_id": b.pid,
            "shot_id": sid_,
            "sub_shot": 1,
            "label": f"{sid_}, keyframe 1",
        }
        r = b.http.post("/v1/experiments", json={"target": target, "reuse_empty": True})
        assert r.status_code == 201, r.text
        chat = r.json()
        view = b.http.get(f"/v1/experiments/{chat['session_id']}").json()
        [start] = view["turns"]
        assert start["mode"] == "pick" and start["from_image"] == f"frame:{take['take_id']}:0"
        # Asked again before writing anything: the same chat.
        again = b.http.post("/v1/experiments", json={"target": target, "reuse_empty": True}).json()
        assert again["session_id"] == chat["session_id"]
        # Starting from a chosen draw, and the chats of a shot.
        other = b.http.post("/v1/experiments", json={
            "target": target, "from_frame": f"{take['take_id']}:0"}).json()  # fmt: skip
        listed = b.http.get("/v1/experiments", params={"shot_id": sid_}).json()
        assert {s["session_id"] for s in listed} == {chat["session_id"], other["session_id"]}
        # It isn't a design picture: no Use as pick.
        assert (
            b.http.post(f"/v1/experiments/{chat['session_id']}/use", json={"image": 1}).status_code
            == 409
        )
        # Use as keyframe: a take of the sub-shot with the chat's image, approved.
        r = b.http.post(f"/v1/experiments/{chat['session_id']}/use-keyframe", json={"image": 1})
        assert r.status_code == 201, r.text
        kt = r.json()
        assert kt["imported_from"] == f"experiment {chat['session_id']} #1"
        assert kt["approved"] == 0 and kt["results"][0]["model"] == "chat"
        assert kt["references"] == take["references"]
        board = b.wait()
        sub = next(s for s in board["shots"] if s["shot_id"] == sid_)["sub_shots"][0]
        assert sub["approved"] == [kt["take_id"], 0]
        done = next(t for t in sub["takes"] if t["take_id"] == kt["take_id"])
        assert done["status"] == "done" and done["results"][0]["check"] is not None
        # Not approved when asked so; the earlier approval stays.
        r = b.http.post(f"/v1/experiments/{chat['session_id']}/use-keyframe",
                        json={"image": 1, "approve": False})  # fmt: skip
        assert r.json()["approved"] is None
        assert b.wait()["shots"] and b.repo.get_shot_take(kt["take_id"]).approved == 0


def test_keyframe_chats_are_checked(tmp_path):
    with Board(tmp_path) as b:
        sid_ = b.board()["shots"][0]["shot_id"]
        url = "/v1/experiments"
        bad = {"package_id": b.pid, "shot_id": sid_, "sub_shot": 9}
        assert b.http.post(url, json={"target": bad}).status_code == 404
        assert (
            b.http.post(
                url, json={"target": bad | {"shot_id": "sh_nope", "sub_shot": 1}}
            ).status_code
            == 404
        )
        ok = {"package_id": b.pid, "shot_id": sid_, "sub_shot": 1}
        assert b.http.post(url, json={"kind": "face", "target": ok}).status_code == 422
        # No draw yet: the chat starts empty, and its images can't become a keyframe
        # before the sub-shot has a take to stand beside.
        chat = b.http.post(url, json={"target": ok}).json()
        assert b.http.get(f"{url}/{chat['session_id']}").json()["turns"] == []
        assert (
            b.http.post(f"{url}/{chat['session_id']}/use-keyframe", json={"image": 1}).status_code
            == 409
        )
