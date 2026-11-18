"""The asset library, phase 1: stories mirrored into characters and sets (with looks, states
and items), picture links, casting, and the read API."""

from drama_agent.library import asset_id, build, sync_library
from drama_agent.sheet_edit import edit_entry
from drama_agent.store import InMemoryRepository
from drama_agent.store.sqlite import SqliteRepository

from .test_shot_frames import Board, done_images


def test_a_story_becomes_characters_and_sets_with_everything_they_own(package):
    images = done_images(package)
    made = build(package, images)
    pid = package.package_id
    by_kind = {}
    for a in made.assets:
        by_kind.setdefault(a.kind, []).append(a)
    bible = package.story_bible
    assert len(by_kind["character"]) == len(bible.characters)
    assert len(by_kind["look"]) == len(bible.looks)
    assert len(by_kind["set"]) == len(bible.locations)
    assert len(by_kind["set_state"]) == len(bible.location_states)
    assert len(by_kind.get("item", [])) == len(bible.props)
    # Looks hang off their character, states off their set, with the sheets and one-liners.
    for lk in bible.looks:
        a = next(x for x in by_kind["look"] if x.origin_entity_id == lk.look_id)
        assert a.parent_id == asset_id("character", pid, lk.character_id)
        assert a.description == lk.description
    for st in bible.location_states:
        a = next(x for x in by_kind["set_state"] if x.origin_entity_id == st.location_state_id)
        assert a.parent_id == asset_id("set", pid, st.location_id) and a.sheet
    # Every story entity is cast, and every design picture is linked to what it shows.
    assert {c.story_id for c in made.casting} == {a.origin_entity_id for a in made.assets}
    reqs = {r.requirement_id: r for r in package.asset_requirements.items}
    designs = [i for i in images if reqs[i.requirement_id].asset_type != "shot_reference"]
    assert {x.image_id for x in made.images} == {i.image_id for i in designs}
    face = next(x for x in made.images if x.requirement_id.startswith("ar_character_identity"))
    assert face.asset_id.startswith("chr_") and face.view == "front face"


def test_props_go_to_whoever_uses_them(package):
    bible = package.story_bible
    owned = next(p for p in bible.props if p.owner_character_id) if any(
        p.owner_character_id for p in bible.props) else None  # fmt: skip
    made = build(package, [])
    items = {a.origin_entity_id: a for a in made.assets if a.kind == "item"}
    for p in bible.props:
        a = items[p.prop_id]
        if p.owner_character_id:
            assert a.parent_id == asset_id("character", package.package_id, p.owner_character_id)
        elif a.parent_id is not None:
            assert a.parent_id.startswith("set_")  # the set it dresses
        assert [s["state_id"] for s in a.states] == [
            s.prop_state_id for s in bible.prop_states if s.prop_id == p.prop_id]  # fmt: skip
    if owned is None:  # the sample has no carried prop: give one an owner and check again
        p = bible.props[0]
        p.owner_character_id = bible.characters[0].character_id
        a = next(x for x in build(package, []).assets if x.origin_entity_id == p.prop_id)
        assert a.parent_id == asset_id("character", package.package_id, p.owner_character_id)


def test_sync_is_idempotent_and_follows_hand_edits(package):
    repo = InMemoryRepository()
    repo.save_package(package, published=True)
    first = sync_library(repo)
    assert first.stories == 1 and first.added > 0
    again = sync_library(repo)
    assert (again.added, again.updated, again.pictures) == (0, 0, 0)  # unchanged: skipped
    forced = sync_library(repo, force=True)
    assert (forced.added, forced.updated) == (0, 0)  # same text: nothing rewritten
    loc = package.set_sheet.locations[0]
    pkg = repo.get_package(package.package_id)
    edit_entry(pkg, loc.location_id, sheet={"layout": "A different room"})
    repo.replace_package(pkg)
    after = sync_library(repo)
    assert after.updated >= 1
    a = repo.get_library_asset(asset_id("set", package.package_id, loc.location_id))
    assert a.sheet["layout"] == "A different room" and a.updated_at >= a.created_at


def test_the_sqlite_store_keeps_the_library(tmp_path, package):
    repo = SqliteRepository(tmp_path / "lib.db")
    repo.save_package(package, published=True)
    sync_library(repo, force=True)
    again = SqliteRepository(tmp_path / "lib.db")  # reopened
    chars = again.list_library_assets(kind="character")
    assert len(chars) == len(package.story_bible.characters)
    first = chars[0]
    assert again.get_library_asset(first.asset_id) == first
    looks = again.list_library_assets(kind="look", parent_id=first.asset_id)
    assert all(x.parent_id == first.asset_id for x in looks)
    assert {c.story_id for c in again.list_casting(package_id=package.package_id)} >= {
        c.character_id for c in package.story_bible.characters}  # fmt: skip
    assert again.list_package_ids() == [package.package_id]


def test_the_library_api_reads_characters_sets_and_casting(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        chars = b.http.get("/v1/library/characters").json()
        pkg = b.repo.get_package(b.pid)
        assert {c["name"] for c in chars} == {c.name for c in pkg.story_bible.characters}
        one = next(c for c in chars if c["pictures"])
        assert one["cover"] and one["children"] >= 1 and one["stories"] == 1
        view = b.http.get(f"/v1/library/characters/{one['asset_id']}").json()
        assert view["looks"] and all(lk["pictures"] for lk in view["looks"])
        story = view["used_in"][0]
        assert story["package_id"] == b.pid and story["shots"]
        sets = b.http.get("/v1/library/sets").json()
        assert sets and all(s["cover"] for s in sets)
        place = b.http.get(f"/v1/library/sets/{sets[0]['asset_id']}").json()
        assert place["states"] and place["states"][0]["pictures"][0]["view"] == "wide"
        assert all(p["outdated"] == [] for st in place["states"] for p in st["pictures"])
        # After a design edit, the set's pictures say they are out of date.
        loc = pkg.set_sheet.locations[0].location_id
        b.http.patch(f"/v1/drama-packages/{b.pid}/entries/{loc}",
                     json={"revision_id": pkg.revision_id, "sheet": {"layout": "A new room"}})  # fmt: skip
        mine = next(s for s in sets if s["origin_entity_id"] == loc)
        place = b.http.get(f"/v1/library/sets/{mine['asset_id']}").json()
        assert place["sheet"]["layout"] == "A new room"
        assert any(p["outdated"] for st in place["states"] for p in st["pictures"])
        # A character id asked for as a set is not found.
        assert b.http.get(f"/v1/library/sets/{one['asset_id']}").status_code == 404
        casting = b.http.get(f"/v1/drama-packages/{b.pid}/casting").json()
        assert {c["story_id"] for c in casting} >= {
            c.character_id for c in pkg.story_bible.characters
        }
        assert all(c["asset_name"] for c in casting)
        assert b.http.post("/v1/library/sync").json()["stories"] == 1


def test_a_redraw_can_be_limited_to_one_asset(tmp_path):
    from .test_redesign import images_idle

    with Board(tmp_path) as b:
        b.design()
        pkg = b.repo.get_package(b.pid)
        first, second = (x.location_id for x in pkg.set_sheet.locations[:2])
        for loc in (first, second):
            r = b.http.patch(f"/v1/drama-packages/{b.pid}/entries/{loc}",
                             json={"revision_id": pkg.revision_id, "sheet": {"layout": f"New {loc}"}})  # fmt: skip
            assert r.status_code == 200, r.text
        mine = [r.requirement_id for r in pkg.asset_requirements.items
                if r.asset_type == "location" and r.subject_refs[0].entity_id == first]  # fmt: skip
        q = "&".join(f"requirement_ids={x}" for x in mine)
        r = b.http.post(f"/v1/drama-packages/{b.pid}/images/refresh?{q}")
        assert r.status_code == 202, r.text
        v = images_idle(b)
        redrawn = {i["requirement_id"] for i in v["images"] if i.get("refreshes")}
        assert redrawn and redrawn <= set(mine)
        # The other set's pictures are still out of date, untouched.
        theirs = {i["image_id"] for i in v["images"] if second in i["requirement_id"]}
        assert theirs & set(v["outdated"])


def test_names_match_across_scripts_and_spellings():
    from drama_agent.library.matching import score

    # The same CJK name with full-width and with ASCII brackets (escaped: the repo is English).
    full, ascii_ = "\u5468\u53d4\uff08\u6797\u5efa\u56fd\uff09", "\u5468\u53d4(\u6797\u5efa\u56fd)"
    assert score(full, ascii_)[1] == "same name"
    assert score("Lila Montgomery", "lila montgomery")[0] == 1.0
    assert score("Lila Montgomery", "Lila Grant")[1] == "same first name"
    assert (
        score("Derek's apartment", "Derek's apartment, morning")[1] == "one name contains the other"
    )
    assert score("Lila", "Derek") is None and score("", "Lila") is None


def second_story(b: Board) -> str:
    from .conftest import wait_job
    from .test_shot_frames import BODY

    job = wait_job(b.http, b.http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
    return job["package_id"]


def test_a_story_can_cast_another_storys_character_and_undo_it(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        other = second_story(b)
        pkg = b.repo.get_package(b.pid)
        who = pkg.story_bible.characters[0]
        found = b.http.get(f"/v1/drama-packages/{other}/casting/matches").json()
        mine = next(m for m in found if m["story_id"] == who.character_id)
        assert mine["linked"] is None
        best = mine["candidates"][0]
        assert best["reason"] == "same name" and best["package_id"] == b.pid
        # Link: the second story uses the first story's character.
        r = b.http.post(f"/v1/drama-packages/{other}/casting",
                        json={"story_id": who.character_id, "asset_id": best["asset_id"]})  # fmt: skip
        assert r.status_code == 200, r.text
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        assert names.count(who.name) == 1  # its own copy is hidden
        shared = b.http.get(f"/v1/library/characters/{best['asset_id']}").json()
        assert {u["package_id"] for u in shared["used_in"]} == {b.pid, other}
        stories = {lk["origin_package_id"] for lk in shared["looks"]}
        assert stories == {b.pid, other}  # its looks joined the shared character
        assert [x["package_id"] for x in shared["also"]] == [other]
        again = b.http.get(f"/v1/drama-packages/{other}/casting/matches").json()
        assert (
            next(m for m in again if m["story_id"] == who.character_id)["linked"]["asset_id"]
            == best["asset_id"]
        )
        # A sync keeps the link.
        b.http.post("/v1/library/sync", params={"force": True})
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        assert names.count(who.name) == 1
        # Unlink: the second story's character is its own again.
        assert (
            b.http.delete(f"/v1/drama-packages/{other}/casting/{who.character_id}").status_code
            == 200
        )
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        assert names.count(who.name) == 2
        shared = b.http.get(f"/v1/library/characters/{best['asset_id']}").json()
        assert {lk["origin_package_id"] for lk in shared["looks"]} == {b.pid} and shared[
            "also"
        ] == []


def test_links_are_checked(tmp_path):
    with Board(tmp_path) as b:
        other = second_story(b)
        pkg = b.repo.get_package(b.pid)
        who = pkg.story_bible.characters[0].character_id
        place = pkg.story_bible.locations[0].location_id
        sets = {s["origin_package_id"]: s for s in b.http.get("/v1/library/sets").json()
                if s["origin_entity_id"] == place}  # fmt: skip
        chars = {c["origin_package_id"]: c for c in b.http.get("/v1/library/characters").json()
                 if c["origin_entity_id"] == who}  # fmt: skip
        url = f"/v1/drama-packages/{other}/casting"
        # A character can't be cast as a set, nor as its own story's asset.
        assert (
            b.http.post(
                url, json={"story_id": who, "asset_id": sets[b.pid]["asset_id"]}
            ).status_code
            == 404
        )
        assert (
            b.http.post(
                url, json={"story_id": who, "asset_id": chars[other]["asset_id"]}
            ).status_code
            == 409
        )
        # Nor as one that is itself linked elsewhere.
        assert (
            b.http.post(
                url, json={"story_id": who, "asset_id": chars[b.pid]["asset_id"]}
            ).status_code
            == 200
        )
        back = f"/v1/drama-packages/{b.pid}/casting"
        assert (
            b.http.post(
                back, json={"story_id": who, "asset_id": chars[other]["asset_id"]}
            ).status_code
            == 409
        )
        assert b.http.delete(f"{url}/{place}").status_code == 404  # not linked


def test_an_archived_storys_assets_are_kept_but_not_shown(tmp_path):
    with Board(tmp_path) as b:
        other = second_story(b)
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        first = b.repo.get_package(b.pid).story_bible.characters[0].name
        assert names.count(first) == 2
        for job in b.repo.jobs_for_package(other):
            job.archived = True
            b.repo.save_job(job)
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        assert names.count(first) == 1
        found = b.http.get(f"/v1/drama-packages/{b.pid}/casting/matches").json()
        assert all(not m["candidates"] for m in found)  # the archived story's are not offered
        for job in b.repo.jobs_for_package(other):
            job.archived = False
            b.repo.save_job(job)
        names = [c["name"] for c in b.http.get("/v1/library/characters").json()]
        assert names.count(first) == 2  # back when it is un-archived


def test_a_linked_story_can_use_the_shared_face_and_redraw_from_it(tmp_path):
    import time

    with Board(tmp_path) as b:
        b.design()
        other = second_story(b)
        b.http.post(f"/v1/drama-packages/{other}/images")
        deadline = time.monotonic() + 30
        while b.http.get(f"/v1/drama-packages/{other}/images").json()["queued"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        pkg = b.repo.get_package(b.pid)
        who = next(c for c in pkg.story_bible.characters
                   if any(lk.character_id == c.character_id for lk in pkg.story_bible.looks))  # fmt: skip
        url = f"/v1/drama-packages/{other}/casting"
        # Not linked yet: nothing to reuse.
        assert b.http.post(f"{url}/{who.character_id}/reuse").status_code == 409
        shared = next(c for c in b.http.get("/v1/library/characters").json()
                      if c["origin_package_id"] == b.pid and c["origin_entity_id"] == who.character_id)  # fmt: skip
        b.http.post(url, json={"story_id": who.character_id, "asset_id": shared["asset_id"]})
        r = b.http.post(f"{url}/{who.character_id}/reuse")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["from_image_id"] == shared["pictures"][0]["image_id"]
        assert out["outdated"] >= 1  # its look was drawn from its old face
        imgs = b.http.get(f"/v1/drama-packages/{other}/images").json()
        face = next(i for i in imgs["images"] if i["image_id"] == out["image_id"])
        assert face["selected"] and face["imported_from"].startswith("library ")
        assert b.media.path(face["file"]).read_bytes() == b.media.path(
            next(i for i in b.repo.list_images(b.pid, pkg.revision_id)
                 if i.image_id == out["from_image_id"]).file).read_bytes()  # fmt: skip
        stale = [i for i in imgs["images"] if i["image_id"] in imgs["outdated"]]
        assert any("character_look" in i["requirement_id"] and who.character_id in i["requirement_id"]
                   for i in stale)  # fmt: skip
        # Redrawn, the look is drawn from the shared face, and nothing is out of date.
        b.http.post(f"/v1/drama-packages/{other}/images/refresh")
        deadline = time.monotonic() + 30
        while (v := b.http.get(f"/v1/drama-packages/{other}/images").json())["queued"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert not any(who.character_id in i["requirement_id"]
                       for i in v["images"] if i["image_id"] in v["outdated"])  # fmt: skip
        redrawn = [i for i in v["images"] if i.get("refreshes") and "character_look" in i["requirement_id"]
                   and who.character_id in i["requirement_id"]]  # fmt: skip
        assert redrawn and out["image_id"] in redrawn[0]["reference_ids"]


def test_a_linked_set_can_use_the_shared_set_view(tmp_path):
    import time

    with Board(tmp_path) as b:
        b.design()
        other = second_story(b)
        b.http.post(f"/v1/drama-packages/{other}/images")
        deadline = time.monotonic() + 30
        while b.http.get(f"/v1/drama-packages/{other}/images").json()["queued"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        place = b.repo.get_package(b.pid).story_bible.locations[0].location_id
        shared = next(s for s in b.http.get("/v1/library/sets").json()
                      if s["origin_package_id"] == b.pid and s["origin_entity_id"] == place)  # fmt: skip
        url = f"/v1/drama-packages/{other}/casting"
        b.http.post(url, json={"story_id": place, "asset_id": shared["asset_id"]})
        r = b.http.post(f"{url}/{place}/reuse")
        assert r.status_code == 200, r.text
        imgs = b.http.get(f"/v1/drama-packages/{other}/images").json()
        view = next(i for i in imgs["images"] if i["image_id"] == r.json()["image_id"])
        assert view["selected"] and view["deliverable_id"].endswith("_wide")
