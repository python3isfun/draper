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
