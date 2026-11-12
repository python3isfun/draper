"""Production hand-off zip: story, scripts, storyboard, design sheets, assets, images."""

import csv
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.media import MediaStore, ffmpeg_available

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}


def wait_images(http, base, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        view = http.get(base).json()
        if not view["pending"] and not view["running"]:
            return view
        assert time.monotonic() < deadline, view
        time.sleep(0.05)


def read_csv(z: zipfile.ZipFile, name: str) -> list[dict]:
    text = z.read(name).decode("utf-8")
    assert text.startswith("﻿")  # BOM, so spreadsheet apps read UTF-8
    return list(csv.DictReader(io.StringIO(text[1:])))


def test_export_zip_has_everything_for_production(tmp_path):
    app = create_app(media=MediaStore(tmp_path), poll_s=0.01)
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        pkg_id, rev = job["package_id"], job["revision_id"]
        images = f"/v1/drama-packages/{pkg_id}/images"
        http.post(images, params={"draft": True, "job_id": job["job_id"]})
        n_images = wait_images(http, images)["done"]

        r = http.get(f"/v1/drama-packages/{pkg_id}/export", params={"job_id": job["job_id"]})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        root = f"the-last-month-s-rent-{rev}"
        assert f'filename="{root}.zip"' in r.headers["content-disposition"]

    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert all(n.startswith(f"{root}/") for n in names)
    expected = {
        "00_README.md", "01_story/story_bible.md", "02_script/episode_01.md",
        "02_script/episode_03.md", "02_script/script.json", "03_storyboard/shot_list.csv",
        "03_storyboard/storyboard.md", "03_storyboard/dialogue_schedule.csv",
        "04_design/cast_sheet.md", "04_design/set_sheet.md", "04_design/prop_sheet.md",
        "05_assets/asset_requirements.csv", "05_assets/reference_images/index.csv",
        "06_qa/qa_report.md", "07_sources/sources.md", "package.json",
    }  # fmt: skip
    assert {f"{root}/{e}" for e in expected} <= names

    pkg = json.loads(z.read(f"{root}/package.json"))
    shots = read_csv(z, f"{root}/03_storyboard/shot_list.csv")
    assert len(shots) == len(pkg["shot_list"]) == 24
    first = shots[0]
    assert (first["episode"], first["scene"], first["shot"]) == ("1", "1", "1")
    assert first["start"] == "00:00.0" and first["duration_s"] == "15"
    assert "Collins Used Books" in first["location"]  # names, not IDs
    assert "Derek Ross: \"You're out by the end of the month." in first["dialogue"]

    script = z.read(f"{root}/02_script/episode_01.md").decode()
    assert "## 1. INT. COLLINS USED BOOKS - DAY" in script and "**DEREK ROSS**" in script
    ep2 = z.read(f"{root}/02_script/episode_02.md").decode()
    assert "**LINDA COLLINS (V.O.)**" in ep2

    index = read_csv(z, f"{root}/05_assets/reference_images/index.csv")
    assert len(index) == n_images > 0
    image_files = [n for n in names if "/reference_images/" in n and not n.endswith(".csv")]
    assert len(image_files) == n_images
    assert z.getinfo(image_files[0]).compress_type == zipfile.ZIP_STORED  # already compressed
    assets = read_csv(z, f"{root}/05_assets/asset_requirements.csv")
    face = next(a for a in assets if a["requirement_id"] == "ar_character_identity_ch_frank")
    assert face["subject"] == "Frank Collins" and face["reference_images"].endswith(".png")

    readme = z.read(f"{root}/00_README.md").decode()
    assert "The Last Month's Rent" in readme and f"{n_images} reference images" in readme
    assert SAMPLE_SEED in z.read(f"{root}/07_sources/sources.md").decode()
    assert "Hard checks: all passed" in z.read(f"{root}/06_qa/qa_report.md").decode()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_export_includes_uploaded_sources(tmp_path):
    import subprocess

    photo = tmp_path / "street.png"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=320x240", "-frames:v", "1", str(photo)],
        check=True,
    )  # fmt: skip
    with TestClient(create_app(media=MediaStore(tmp_path / "media"), poll_s=0.01)) as http:
        asset = http.post(
            "/v1/source-assets", params={"filename": "street.png"}, content=photo.read_bytes()
        ).json()
        body = {"sources": [{"type": "image", "asset_id": asset["asset_id"],
                             "text": "make her the lead in a revenge story"}]}  # fmt: skip
        job = wait_job(http, http.post("/v1/drama-jobs", json=body).json()["job_id"])
        r = http.get(
            f"/v1/drama-packages/{job['package_id']}/export", params={"job_id": job["job_id"]}
        )
    z = zipfile.ZipFile(io.BytesIO(r.content))
    sources = [n for n in z.namelist() if "/07_sources/" in n]
    assert any(n.endswith(f"01_{asset['asset_id']}.png") for n in sources)
    md = z.read(next(n for n in sources if n.endswith("sources.md"))).decode()
    assert "Prompt: make her the lead in a revenge story" in md and "street.png" in md


def test_export_refuses_archived_and_unknown_packages(tmp_path):
    app = create_app(media=MediaStore(tmp_path), poll_s=0.01)
    with TestClient(app) as http:
        job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        assert http.get("/v1/drama-packages/pkg_nope/export").status_code == 404
        stored = app.state.repo.get_job(job["job_id"])
        stored.archived = True
        app.state.repo.save_job(stored)
        assert http.get(f"/v1/drama-packages/{job['package_id']}/export").status_code == 409


def test_timestamps_never_round_into_an_invalid_minute():
    from drama_agent.export import ts

    assert ts(59_960) == "00:59.9" and ts(60_000) == "01:00.0" and ts(119_999) == "01:59.9"


def test_images_are_named_per_deliverable_and_only_existing_files_are_listed(package, tmp_path):
    from drama_agent.export import build_export, image_name
    from drama_agent.images import plan_images

    media = MediaStore(tmp_path / "media")
    plan = plan_images(package, [])
    same_view = [i for i in plan if i.requirement_id.startswith("ar_location_loc_shop")][:2]
    assert image_name(same_view[0]) != image_name(same_view[1])

    good, missing, outside = plan[0], plan[1], plan[2]
    for img in (good, missing, outside):
        img.status = "done"
    good.file = "generated/good.jpg"
    media.path(good.file).parent.mkdir(parents=True)
    media.path(good.file).write_bytes(b"\xff\xd8\xffJPEG")
    missing.file = "generated/gone.jpg"
    outside.file = "../../outside.jpg"  # resolves outside the media store

    dest = tmp_path / "out.zip"
    build_export(package, dest, job=None, images=[good, missing, outside], assets={}, media=media)
    z = zipfile.ZipFile(dest)
    root = z.namelist()[0].split("/")[0]
    images = [n for n in z.namelist() if "/reference_images/" in n and n.endswith(".jpg")]
    assert [n.rsplit("/", 1)[1] for n in images] == [image_name(good)]
    listed = ",".join(
        a["reference_images"] for a in read_csv(z, f"{root}/05_assets/asset_requirements.csv")
    )
    assert image_name(good) in listed and "gone" not in listed and "outside" not in listed
    assert "1 reference images" in z.read(f"{root}/00_README.md").decode()


def test_qa_note_names_unverified_episodes_and_speakers_fall_back_to_ids(package):
    from drama_agent.export import Names, dialogue_rows, qa_md

    assert "not verified for any episode" in qa_md(package)
    package.timing[0].spoken_timing_verified = True
    package.timing[2].spoken_timing_verified = True
    assert "not verified for episode 2 yet" in qa_md(package)

    package.episodes[0].scenes[0].beats[0].dialogue.character_id = "ch_unknown"
    rows = dialogue_rows(package, Names(package))
    assert rows[0]["character"] == "ch_unknown"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_jobs_for_package_finds_the_job_without_scanning_the_list(kind, tmp_path):
    from drama_agent.store import InMemoryRepository
    from drama_agent.store.sqlite import SqliteRepository

    from .conftest import run_pipeline

    job, _ = run_pipeline()
    repo = InMemoryRepository() if kind == "memory" else SqliteRepository(tmp_path / "t.db")
    repo.save_job(job)
    assert [j.job_id for j in repo.jobs_for_package(job.package_id)] == [job.job_id]
    assert repo.jobs_for_package("pkg_nope") == []


def test_export_without_job_id_and_old_temp_files_are_swept(tmp_path):
    import os

    from drama_agent.api import app as app_module

    app_module.EXPORT_DIR = tmp_path / "exports"
    app_module.EXPORT_DIR.mkdir()
    stale = app_module.EXPORT_DIR / "drama-export-stale.zip"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    try:
        with TestClient(create_app(media=MediaStore(tmp_path / "m"), poll_s=0.01)) as http:
            job = wait_job(http, http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
            r = http.get(f"/v1/drama-packages/{job['package_id']}/export")  # no job_id
        z = zipfile.ZipFile(io.BytesIO(r.content))
        sources = next(n for n in z.namelist() if n.endswith("07_sources/sources.md"))
        assert SAMPLE_SEED in z.read(sources).decode()  # found the job by package
        assert not stale.exists()  # swept
        assert list(app_module.EXPORT_DIR.iterdir()) == []  # this export's file deleted too
    finally:
        app_module.EXPORT_DIR = app_module.Path(app_module.tempfile.gettempdir()) / "drama-exports"
