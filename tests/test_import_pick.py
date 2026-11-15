"""Experiment images as design-board picks, and how a likeness carries over."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.content_guard import GuardRefused
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import FixtureImageClient
from drama_agent.images import ImageWorker, develop, import_pick, intimate_looks
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.models import Intimacy
from drama_agent.models.images import GeneratedImage
from drama_agent.store import InMemoryRepository

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}
FRANK_FACE = ("ar_character_identity_ch_frank", "d_ar_character_identity_ch_frank_face")


def frank_look_scenes(pkg):
    """(look_id, scene_id) pairs for Frank's looks in the sample package."""
    return sorted(
        {(r.look_id, sh.scene_id) for sh in pkg.shot_list for r in sh.character_look_refs
         if r.character_id == "ch_frank"}
    )  # fmt: skip


def picked_face(pkg, *, likeness: bool) -> GeneratedImage:
    img = import_pick(
        pkg, FRANK_FACE, file="generated/x/face.png", thumb=None, content_type="image/png",
        prompt="a man", likeness=likeness, imported_from="experiment exp_x #3", job_id=None,
    )  # fmt: skip
    img.selected = True
    return img


def make_intimate(pkg, look_id: str, level: str = "implied") -> None:
    """Every scene where Frank wears `look_id` gets an intimacy block including Frank."""
    for sc in (sc for ep in pkg.episodes for sc in ep.scenes):
        if any(s == sc.scene_id for lk, s in frank_look_scenes(pkg) if lk == look_id):
            sc.intimacy = Intimacy(level=level, character_ids=["ch_frank"])


def costume_only_look(pkg) -> tuple[str, str]:
    """(a Frank look whose scenes have no other Frank look, another Frank look)."""
    pairs = frank_look_scenes(pkg)
    for lk, _ in pairs:
        scenes = {s for k, s in pairs if k == lk}
        if not any(k != lk and s in scenes for k, s in pairs):
            return lk, next(k for k, _ in pairs if k != lk)
    raise AssertionError(f"no look with scenes of its own: {pairs}")


def frank_looks(images) -> set[str]:
    return {
        i.requirement_id
        for i in images
        if i.requirement_id.startswith("ar_character_look_ch_frank")
    }


def test_a_likeness_face_does_not_develop_its_intimate_scene_looks(package):
    costume, other = costume_only_look(package)
    make_intimate(package, costume)
    _, imgs, _, notes = develop(
        package, [], [picked_face(package, likeness=True)], draws=1, draft=True, job_id=None
    )
    developed = frank_looks(imgs)
    assert f"ar_character_look_ch_frank_{costume}" not in developed
    assert f"ar_character_look_ch_frank_{other}" in developed
    assert len(notes) == 1 and "only in intimate scenes" in notes[0] and "likeness" in notes[0]
    label = costume.removeprefix("lk_frank_").replace("_", " ")
    assert notes[0].startswith(f"Frank Collins, {label} look, full body")


def test_a_look_also_worn_outside_intimate_scenes_is_drawn(package):
    """Seen live: Lila dresses for work during the morning-after scene; her work outfit is
    everyday clothing and is drawn."""
    costume, other = costume_only_look(package)
    make_intimate(package, costume)
    pairs = frank_look_scenes(package)
    other_scene = next(s for k, s in pairs if k == other)
    # Frank also wears the costume look in a scene with no intimacy.
    sh = next(sh for sh in package.shot_list if sh.scene_id == other_scene)
    ref = next(r for r in sh.character_look_refs if r.character_id == "ch_frank")
    ref.look_id = costume
    intimate = intimate_looks(package)
    assert ("ch_frank", costume) not in intimate


def test_a_face_without_likeness_develops_every_look(package):
    looks = sorted({lk for lk, _ in frank_look_scenes(package)})
    make_intimate(package, costume_only_look(package)[0])
    _, imgs, _, notes = develop(
        package, [], [picked_face(package, likeness=False)], draws=1, draft=True, job_id=None
    )
    assert frank_looks(imgs) == {f"ar_character_look_ch_frank_{lk}" for lk in looks}
    assert notes == []


def test_images_drawn_from_a_likeness_get_it_and_the_stricter_rules(package, tmp_path):
    repo, media = InMemoryRepository(), MediaStore(tmp_path)
    repo.save_package(package, published=True)
    face = picked_face(package, likeness=True)
    media.path(face.file).parent.mkdir(parents=True, exist_ok=True)
    media.path(face.file).write_bytes(b"\x89PNG fake")
    repo.save_image(face)
    worker = ImageWorker(repo, media, FixtureImageClient(), writer=None)
    ok = face.model_copy(
        update={"image_id": "img_ok", "status": "pending", "file": None, "selected": False,
                "likeness": False, "imported_from": None,
                "reference_ids": [f"sel:{FRANK_FACE[0]}|{FRANK_FACE[1]}"],
                "prompt": "<Picture 1> is <Subject 1>. The same man in a grey suit."}
    )  # fmt: skip
    asyncio.run(worker._generate(ok))
    assert ok.status == "done" and ok.likeness  # inherited from its reference
    risky = ok.model_copy(
        update={"image_id": "img_risky", "status": "pending", "file": None, "likeness": False,
                "prompt": "<Picture 1> is <Subject 1>. The same man in his underwear."}
    )  # fmt: skip
    with pytest.raises(GuardRefused, match="Not drawn"):
        asyncio.run(worker._generate(risky))


def test_import_an_experiment_image_as_a_pick(tmp_path):
    app = create_app(
        client=FixtureModelClient(), image_client=FixtureImageClient(),
        media=MediaStore(tmp_path), poll_s=0.01,
    )  # fmt: skip
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        sid = http.post("/v1/experiments", json={}).json()["session_id"]
        http.post(f"/v1/experiments/{sid}/turns", json={"text": "a man in a grey coat"})
        deadline = time.monotonic() + 10
        while http.get(f"/v1/experiments/{sid}").json()["turns"][-1]["status"] != "done":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        base = f"/v1/drama-packages/{job['package_id']}/images"
        body = {"session_id": sid, "image": 1, "requirement_id": FRANK_FACE[0]}
        r = http.post(f"{base}/import", json=body)
        assert r.status_code == 201, r.text
        img = r.json()
        assert img["selected"] and img["status"] == "done"
        assert img["deliverable_id"] == FRANK_FACE[1]
        assert img["imported_from"] == f"experiment {sid} #1"
        assert img["likeness"] is False  # the Experiment judged it: no real person
        assert http.get(f"/v1/generated-images/{img['image_id']}/content").status_code == 200
        # The looks are developed from it.
        v = http.post(f"{base}/develop", json={"job_id": job["job_id"], "draws": 1}).json()
        assert any(
            i["requirement_id"].startswith("ar_character_look_ch_frank") for i in v["images"]
        )
        # Override and errors.
        r = http.post(f"{base}/import", json={**body, "likeness": True})
        assert r.json()["likeness"] is True
        assert http.post(f"{base}/import", json={**body, "image": 99}).status_code == 404
        r = http.post(f"{base}/import", json={**body, "requirement_id": "ar_nope"})
        assert r.status_code == 404


def test_a_kiss_scene_costume_is_drawn_from_a_likeness_face(package):
    looks = sorted({lk for lk, _ in frank_look_scenes(package)})
    make_intimate(package, costume_only_look(package)[0], level="kiss")
    _, imgs, _, notes = develop(
        package, [], [picked_face(package, likeness=True)], draws=1, draft=True, job_id=None
    )
    assert frank_looks(imgs) == {f"ar_character_look_ch_frank_{lk}" for lk in looks}
    assert notes == []
