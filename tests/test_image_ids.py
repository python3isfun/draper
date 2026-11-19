"""Global image numbers (@1234): every picture has one, and a chat message can bring any
picture in by it (docs/image-ids.md)."""

from drama_agent.experiment import chat_text, global_mentions
from drama_agent.image_ids import parse
from tests.test_experiment import PORTRAIT, Chat


def test_mentions_and_chat_text() -> None:
    text = 'like @12 but with @7\'s coat, "sign @3", mail a@4, @12 again'
    assert global_mentions(text) == [12, 7]
    assert chat_text(text, {"12": 5, "7": 6}) == (
        'like #5 but with #6\'s coat, "sign @3", mail a@4, #5 again'
    )
    assert parse("turn:turn_x:2") == ("turn", "turn_x", 2)
    assert parse("design:img_1") == ("design", "img_1", 0)
    assert parse("take:t:x") is None and parse("nope:1") is None


def test_numbers_are_stable_and_resolve(tmp_path) -> None:
    with Chat(tmp_path) as c:
        c.send(text="a red cup", mode="exact")
        v = c.wait()
        ref = f"turn:{v['turns'][0]['turn_id']}:0"
        first = c.http.post("/v1/image-ids", json={"refs": [ref]}).json()
        again = c.http.post("/v1/image-ids", json={"refs": ["asset:asset_x", ref]}).json()
        assert again[ref] == first[ref] and again["asset:asset_x"] != first[ref]
        info = c.http.get(f"/v1/images/{first[ref]}").json()
        assert info["ref"] == ref and info["what"].startswith("image #1 of the chat")
        assert c.http.get(info["thumb_url"]).status_code == 200
        assert c.http.get(f"/v1/images/{again['asset:asset_x']}").status_code == 404
        assert c.http.post("/v1/image-ids", json={"refs": ["bad"]}).status_code == 422


def test_a_message_brings_in_an_image_from_another_chat(tmp_path) -> None:
    with Chat(tmp_path) as c:
        c.send(text="a red cup", mode="exact")
        v = c.wait()
        ref = f"turn:{v['turns'][0]['turn_id']}:0"
        gid = c.http.post("/v1/image-ids", json={"refs": [ref]}).json()[ref]

        other = c.http.post("/v1/experiments", json={}).json()["session_id"]
        c.sid = other
        c.send(text="a blue plate", mode="exact")
        c.wait()
        r = c.send(text=f"put the cup from @{gid} on <Picture 1> plate", mode="exact")
        assert r.status_code == 202, r.text
        v = c.wait()
        imp, msg = v["turns"][1], v["turns"][2]
        assert imp["mode"] == "import" and imp["import_ref"] == ref
        assert imp["text"].startswith(f"Brought in @{gid}:")
        label = imp["results"][0]["label"]
        assert msg["global_mentions"] == {str(gid): label}
        assert msg["references"][1] == f"turn:{imp['turn_id']}:0"
        assert "<Picture 2>" in msg["prompt"] and "@" not in msg["prompt"]
        assert msg["status"] == "done"
        # Mentioned again: the image already brought in is reused.
        c.send(text=f"again with @{gid}", mode="exact")
        v = c.wait()
        assert [t["mode"] for t in v["turns"]].count("import") == 1
        # Messages count only what the user wrote.
        listed = {s["session_id"]: s for s in c.http.get("/v1/experiments").json()}
        assert listed[other]["messages"] == 3


def test_an_unknown_number_is_refused_and_leaves_nothing(tmp_path) -> None:
    with Chat(tmp_path) as c:
        r = c.send(text="like @999", mode="exact")
        assert r.status_code == 422 and "@999" in r.json()["detail"]
        assert c.view()["turns"] == []


def test_a_chat_image_mentioned_in_its_own_chat_is_not_copied(tmp_path) -> None:
    with Chat(tmp_path) as c:
        c.send(text="a red cup", mode="exact")
        v = c.wait()
        ref = f"turn:{v['turns'][0]['turn_id']}:0"
        gid = c.http.post("/v1/image-ids", json={"refs": [ref]}).json()[ref]
        c.send(text=f"like @{gid} but green", mode="exact")
        v = c.wait()
        assert [t["mode"] for t in v["turns"]] == ["exact", "exact"]
        assert v["turns"][1]["prompt"] == "like <Picture 1> but green"


def test_sqlite_numbers(tmp_path) -> None:
    from drama_agent.store.sqlite import SqliteRepository

    repo = SqliteRepository(tmp_path / "x.db")
    a = repo.image_ids(["asset:a", "design:b"])
    b = repo.image_ids(["design:b", "take:c:0", "asset:a"])
    assert a["asset:a"] == b["asset:a"] and a["design:b"] == b["design:b"]
    assert len({*a.values(), *b.values()}) == 3
    assert repo.image_ref(b["take:c:0"]) == "take:c:0" and repo.image_ref(999) is None


def test_a_design_image_brings_its_likeness(tmp_path) -> None:
    from drama_agent.models.images import GeneratedImage

    with Chat(tmp_path) as c:
        repo, media = c.app.state.repo, c.app.state.media
        media.path("images/x.png").parent.mkdir(parents=True, exist_ok=True)
        media.path("images/x.png").write_bytes(PORTRAIT)
        repo.save_image(GeneratedImage(
            image_id="img_x", package_id="pkg_none", revision_id="rev_none",
            requirement_id="r", deliverable_id="d", prompt="a woman, front face",
            size="1728x2304", seed=1, status="done", file="images/x.png",
            content_type="image/png", likeness=True,
        ))  # fmt: skip
        gid = c.http.post("/v1/image-ids", json={"refs": ["design:img_x"]}).json()["design:img_x"]
        c.send(text=f"her in a red coat, like @{gid}", mode="exact")
        v = c.wait()
        imp, msg = v["turns"]
        assert imp["likeness"] is True and imp["prompt"] == "a woman, front face"
        assert "design picture (d)" in imp["text"]
        assert msg["likeness"] is True  # inherited from the picture it was drawn from


def test_a_brought_in_picture_keeps_its_shape_and_unknown_likeness(tmp_path) -> None:
    """A 3:4 design picture drawn from nothing real, in a story that may have uploaded
    photos (unknown counts as yes): the message is drawn 3:4, and the picture is judged."""
    from drama_agent.models.images import GeneratedImage

    with Chat(tmp_path) as c:
        repo, media = c.app.state.repo, c.app.state.media
        media.path("images/y.png").parent.mkdir(parents=True, exist_ok=True)
        media.path("images/y.png").write_bytes(PORTRAIT)
        repo.save_image(GeneratedImage(
            image_id="img_y", package_id="pkg_none", revision_id="rev_none",
            requirement_id="r", deliverable_id="d", prompt="a face", size="3552x4736",
            seed=1, status="done", file="images/y.png", content_type="image/png",
        ))  # fmt: skip
        gid = c.http.post("/v1/image-ids", json={"refs": ["design:img_y"]}).json()["design:img_y"]
        c.send(text=f"@{gid} with a hat", mode="exact")
        v = c.wait()
        imp, msg = v["turns"]
        assert imp["likeness"] is None and imp["size"] == "1728x2304"
        assert msg["size"] == "1728x2304"


def _design(c, image_id: str, likeness: bool) -> int:
    from drama_agent.models.images import GeneratedImage

    repo, media = c.app.state.repo, c.app.state.media
    media.path(f"images/{image_id}.png").parent.mkdir(parents=True, exist_ok=True)
    media.path(f"images/{image_id}.png").write_bytes(PORTRAIT)
    repo.save_image(GeneratedImage(
        image_id=image_id, package_id="pkg_none", revision_id="rev_none",
        requirement_id="r", deliverable_id="d", prompt="a face", size="3552x4736",
        seed=1, status="done", file=f"images/{image_id}.png", content_type="image/png",
        likeness=likeness,
    ))  # fmt: skip
    ref = f"design:{image_id}"
    return c.http.post("/v1/image-ids", json={"refs": [ref]}).json()[ref]


def test_a_face_study_describes_a_picture_by_number(tmp_path) -> None:
    from tests.test_face_lab import Study

    with Study(tmp_path) as c:
        gid = _design(c, "img_f", likeness=True)
        r = c.describe(image=gid)
        assert r.status_code == 202, r.text
        v = c.wait()
        [turn] = v["turns"]
        assert turn["mode"] == "describe" and turn["status"] == "done"
        assert turn["likeness"] is True  # kept from the picture, whatever the model says
        assert v["session"]["title"].startswith(f"@{gid}: a design picture")
        [asset] = turn["attachments"]
        raw = c.app.state.repo.get_source_asset(asset)
        assert raw["from_image"] == "design:img_f" and raw["from_likeness"] is True
        # The copy shows the picture's number.
        got = c.http.post("/v1/image-ids", json={"refs": [f"asset:{asset}"]}).json()
        assert got == {f"asset:{asset}": gid}
        # Described again by number (or again as the study's photo): the same copy, and
        # the likeness holds.
        c.describe(image=gid)
        c.wait()
        c.describe(hint="the eyes")
        v = c.wait()
        assert {a for t in v["turns"] for a in t["attachments"]} == {asset}
        assert all(t["likeness"] is True for t in v["turns"])


def test_a_face_study_refuses_unknown_numbers_and_numbers_in_tests(tmp_path) -> None:
    from tests.test_face_lab import Study

    with Study(tmp_path) as c:
        r = c.describe(image=999)
        assert r.status_code == 422 and "@999" in r.json()["detail"]
        r = c.describe(image=1, asset_id=c.upload())
        assert r.status_code == 422
        r = c.test(prompt="a woman like @3")
        assert r.status_code == 422 and "describe it" in r.json()["detail"]
        assert c.view()["turns"] == []


def test_a_face_study_uses_an_upload_by_number_as_it_is(tmp_path) -> None:
    from tests.test_face_lab import Study

    with Study(tmp_path) as c:
        asset = c.upload()
        gid = c.http.post("/v1/image-ids", json={"refs": [f"asset:{asset}"]}).json()
        c.describe(image=gid[f"asset:{asset}"])
        v = c.wait()
        assert v["turns"][0]["attachments"] == [asset]
