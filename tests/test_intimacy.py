"""Intimacy: scene staging within the content rating, adults only, the export's intimacy
plan, and the content guard on image prompts."""

import zipfile

import pytest
from fastapi.testclient import TestClient

from drama_agent.content_guard import (
    MINOR_REASON,
    NUDITY_REASON,
    REAL_PERSON_REASON,
    text_guard,
    upload_guard,
)
from drama_agent.experiment import ExpTurn, picture_kind
from drama_agent.export import build_export
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.image_prompts import problems
from drama_agent.llm import FixtureModelClient
from drama_agent.models import Intimacy, JobOptions
from drama_agent.validation.hard_checks import (
    apparent_adult,
    check_cast_sheet,
    check_intimacy,
    check_intimacy_minors,
    run_hard_checks,
)
from drama_agent.validation.index import PackageIndex

from .conftest import wait_job
from .test_design_board import FRANK_FACE, Session, key
from .test_experiment import PORTRAIT, Chat


def stage(pkg, **kw):
    """Make the first two-character scene intimate; returns (scene, its character ids)."""
    sc = next(s for ep in pkg.episodes for s in ep.scenes if len(s.character_ids) >= 2)
    who = sc.character_ids[:2]
    sc.intimacy = Intimacy(
        **{
            "level": "implied",
            "character_ids": who,
            "on_screen": "A long kiss in the doorway; the door closes.",
            "off_screen": "They spend the night together.",
            "consent_beats": ["She pulls him in and asks 'Stay?'; he nods."],
            **kw,
        }
    )
    return sc, who


def intimacy_issues(pkg, rating):
    return check_intimacy(pkg, PackageIndex.build(pkg), JobOptions(content_rating=rating))


def test_rating_caps_the_staging(package):
    sc, _ = stage(package)
    assert intimacy_issues(package, "teen") == []
    general = intimacy_issues(package, "general")
    assert len(general) == 1 and "at most kiss" in general[0].evidence
    assert general[0].object_id == sc.scene_id and general[0].fix_scope == "script"

    sc.intimacy.level = "simulated"
    assert any("at most implied" in i.evidence for i in intimacy_issues(package, "teen"))
    mature = intimacy_issues(package, "mature")
    assert [i.evidence for i in mature] == ["simulated intimacy or nudity needs closed_set=true"]
    sc.intimacy.closed_set = True
    assert intimacy_issues(package, "mature") == []


def test_nudity_consent_and_staging_are_required(package):
    sc, _ = stage(package, nudity="partial", consent_beats=[], on_screen=" ")
    found = " | ".join(i.evidence for i in intimacy_issues(package, "teen"))
    assert "partial nudity, which the teen rating does not allow" in found
    assert "no consent_beats" in found
    assert "on_screen is empty" in found
    assert "needs closed_set=true" in found
    sc.intimacy.character_ids = ["ch_nobody"]
    assert any("ch_nobody" in i.evidence for i in intimacy_issues(package, "teen"))


def test_default_rating_and_untouched_packages_pass(package):
    assert JobOptions().content_rating == "teen"
    assert intimacy_issues(package, "general") == []  # no scene has intimacy
    assert run_hard_checks(package, JobOptions()) == []


@pytest.mark.parametrize(
    ("age", "adult"),
    [
        ("about 28", True),
        ("late 20s", True),
        ("early thirties", True),
        ("18", True),
        ("about 16", False),
        ("17 but looks older", False),
        ("a teenager", False),
        ("high schooler", False),
        ("young", None),
        ("ageless", None),
        ("about 40, mother of teenage kids", True),
        ("about 28, 5'6\" tall", True),
        ("early 30s, 2 kids", True),
        ("mid-20s, 170 cm", True),
    ],
)
def test_apparent_adult(age, adult):
    assert apparent_adult(age) is adult


def test_minors_in_intimate_scenes_block_the_script_not_the_age(package):
    sc, who = stage(package)
    members = {c.character_id: c for c in package.cast_sheet.characters}
    members[who[0]].age_range = "about 16"
    members[who[1]].age_range = "young"
    idx = PackageIndex.build(package)
    # The cast sheet is only asked for a stated age; it is never told to make anyone older.
    cast = [
        i for i in check_cast_sheet(package, idx, JobOptions()) if i.check.startswith("intimacy")
    ]
    assert [(i.check, i.object_id) for i in cast] == [("intimacy_age", who[1])]
    assert "18" not in cast[0].evidence
    # A minor blocks the job, with the fix in the script.
    minors = check_intimacy_minors(package, idx, JobOptions())
    assert [(i.object_id, i.fix_scope) for i in minors] == [(sc.scene_id, "script")]
    assert "Do not change the character's age" in minors[0].evidence
    assert any(i.check == "intimacy_minor" for i in run_hard_checks(package, JobOptions()))
    # A kiss doesn't involve ages.
    sc.intimacy.level = "kiss"
    assert check_intimacy_minors(package, idx, JobOptions()) == []
    assert not [
        i for i in check_cast_sheet(package, idx, JobOptions()) if i.check == "intimacy_age"
    ]


def test_export_has_an_intimacy_plan_only_when_needed(package, tmp_path):
    def names(pkg, dest):
        build_export(pkg, dest, job=None, images=[], assets={}, media=None)
        with zipfile.ZipFile(dest) as z:
            return {
                n.split("/", 1)[1]: z.read(n).decode() for n in z.namelist() if n.endswith(".md")
            }

    files = names(package, tmp_path / "a.zip")
    assert "08_intimacy/intimacy_plan.md" not in files
    assert "08_intimacy" not in files["00_README.md"]

    stage(package)
    files = names(package, tmp_path / "b.zip")
    plan = files["08_intimacy/intimacy_plan.md"]
    assert "intimacy coordinator" in plan and "Implied (cuts away)" in plan
    assert "A long kiss in the doorway" in plan and "Stay?" in plan
    assert "`08_intimacy/`" in files["00_README.md"]
    assert any(
        "Intimacy: implied" in md for name, md in files.items() if name.startswith("02_script")
    )


# ---------------------------------------------------------------------------
# Content guard


@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        ("A nude woman on a bed", NUDITY_REASON),
        ("She is topless on the beach", NUDITY_REASON),
        ("A sexually explicit scene", NUDITY_REASON),
        ("A teenage girl in lingerie", MINOR_REASON),
        ("A 16-year-old posing in a bikini, seductive look", MINOR_REASON),
        ("A schoolgirl with a sexy pose", MINOR_REASON),
        ("A mother hugging her kids in the kitchen", None),
        ("A man in a striped shirt kisses his wife goodbye", None),
        ("A 28-year-old woman in a black slip dress", None),
        ("A casting photograph of Lila, charcoal blazer", None),
        ("Soft nude lipstick and a low bun.", None),
        ("Nude-colored heels with a black dress, nude tones.", None),
        ("Fully clothed, no nudity. Not naked.", None),
        ("Nude lipstick; she is nude on the bed.", NUDITY_REASON),
    ],
)
def test_text_guard(prompt, reason):
    assert text_guard(prompt) == reason


def test_written_design_prompts_fall_back_when_not_allowed():
    found = problems("A naked man. The image contains no text, letters or numbers.", 0)
    assert any(p.startswith("not allowed") for p in found)


class Refuser(FixtureModelClient):
    """Refuses in the writer and in the content screen."""

    async def generate(self, request, output_model):
        if request.stage in ("experiment", "content_guard"):
            self.calls.append(request)
        if request.stage == "experiment":
            return output_model(allowed=False, refusal="Not with an uploaded photo.")
        if request.stage == "content_guard":
            return output_model(allowed=False, reason="Not with an uploaded photo.")
        return await super().generate(request, output_model)


def test_experiment_writer_refusal_draws_nothing(tmp_path):
    with Chat(tmp_path, text=Refuser()) as c:
        c.send(attachments=[c.upload()])
        c.send(text="put her in a bikini on a bed")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and turn["refused"]
        assert turn["error"] == "Not with an uploaded photo."
        assert c.images.calls == [] and turn["prompt"] == ""
        r = c.http.post(f"/v1/experiment-turns/{turn['turn_id']}/retry")
        assert r.status_code == 409


def test_exact_prompts_are_screened(tmp_path):
    with Chat(tmp_path, text=Refuser()) as c:
        c.send(text="Two adults kiss at a bus stop.", mode="exact")
        turn = c.wait()["turns"][-1]
        assert turn["refused"] and c.images.calls == []
        assert c.text.calls[-1].stage == "content_guard"

    with Chat(tmp_path) as c:  # allowed by the screen: drawn
        c.send(text="Two adults kiss at a bus stop.", mode="exact")
        assert c.wait()["turns"][-1]["status"] == "done"
        # Clear cases are refused without asking the model.
        c.send(text="A naked man on a beach.", mode="exact")
        turn = c.wait()["turns"][-1]
        assert turn["refused"] and turn["error"] == NUDITY_REASON
        assert c.text.calls[-1].stage == "content_guard"  # only the first message's screen
        assert len(c.images.calls) == 1


def test_results_of_an_upload_count_as_uploads(tmp_path):
    with Chat(tmp_path) as c:
        c.send(attachments=[c.upload()])
        c.send(text="make the background blue")
        first = c.wait()["turns"][-1]
        assert first["from_upload"]
        c.send(text="add a hat")
        c.wait()
        kinds = [p["kind"] for p in c.text.calls[-1].context["pictures"]]
        assert kinds == ["uploaded photo, as edited in this chat (may show a real person)"]

        c.http.patch(f"/v1/experiments/{c.sid}", json={"clear_current": True})
        c.send(text="a red bicycle")
        fresh = c.wait()["turns"][-1]
        assert not fresh["from_upload"]


def test_design_board_refuses_explicit_edits(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        face = next(o for o in v["options"] if key(o) == FRANK_FACE)
        url = f"/v1/image-options/{face['option_id']}/edit"
        r = s.http.post(url, json={"prompt": "A topless casting photograph of Frank Collins."})
        assert r.status_code == 422 and r.json()["detail"] == NUDITY_REASON
        assert s.text.calls[-1].stage != "content_guard"  # refused before any model call
        r = s.http.post(url, json={"prompt": face["prompt"].replace("studio", "daylight")})
        assert r.status_code == 202
        assert s.text.calls[-1].stage == "content_guard"


def test_worker_never_draws_a_refused_prompt(tmp_path):
    with Session(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        img = next(i for i in v["images"] if key(i) == FRANK_FACE)
        # Forge a stored take prompt the guard refuses (its draws use the take's prompt),
        # then queue the draw again.
        repo = s.app.state.repo
        opt = repo.get_option(img["option_id"])
        opt.prompt = "A nude portrait."
        repo.save_option(opt)
        stored = repo.get_image(img["image_id"])
        stored.prompt, stored.status, stored.file = "A nude portrait.", "pending", None
        repo.save_image(stored)
        s.wait()
        failed = s.app.state.repo.get_image(img["image_id"])
        assert failed.status == "failed" and NUDITY_REASON in failed.error


def test_upload_guard():
    assert upload_guard("Lila in a black lace bra and panties") == REAL_PERSON_REASON
    assert upload_guard("Two people in bed, morning light") == REAL_PERSON_REASON
    assert upload_guard("A nude portrait") == NUDITY_REASON
    assert upload_guard("Lila in a charcoal blazer, nude lipstick") is None
    assert text_guard("Lila in a red bikini on the beach") is None  # fine without uploads


def test_lineage_follows_references_not_the_stored_flag():
    # Saved before from_upload existed: the flag is False, the references say otherwise.
    old = ExpTurn(
        turn_id="turn_old", session_id="s", index=2, references=["asset:asset_x"], status="done"
    )
    child = ExpTurn(
        turn_id="turn_kid", session_id="s", index=3, references=["turn:turn_old:0"], status="done"
    )
    fresh = ExpTurn(turn_id="turn_txt", session_id="s", index=4, status="done")
    turns = [old, child, fresh]
    assert not old.from_upload
    assert picture_kind("turn:turn_kid:0", turns).startswith("uploaded photo")
    assert picture_kind("turn:turn_txt:0", turns).startswith("generated image")
    assert picture_kind("turn:turn_gone:0", turns).startswith("uploaded photo")


class UploadSession(Session):
    """A design-board session whose job has an uploaded photo (a character reference)."""

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        asset = self.http.post(
            "/v1/source-assets", params={"filename": "me.png"}, content=PORTRAIT
        ).json()
        body = {
            "sources": [
                {"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"},
                {"type": "image", "asset_id": asset["asset_id"], "use_as": "character_reference"},
            ]
        }
        job_id = self.http.post("/v1/drama-jobs", json=body).json()["job_id"]
        self.job = wait_job(self.http, job_id)
        self.base = f"/v1/drama-packages/{self.job['package_id']}/images"
        return self


def test_design_board_applies_the_uploaded_photo_rule(tmp_path):
    with UploadSession(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        face = next(o for o in v["options"] if key(o) == FRANK_FACE)
        url = f"/v1/image-options/{face['option_id']}/edit"
        r = s.http.post(url, json={"prompt": "Frank Collins in swimwear on a beach."})
        assert r.status_code == 422 and r.json()["detail"] == REAL_PERSON_REASON
        # The worker applies it to every prompt, however it was written.
        repo = s.app.state.repo
        img = next(i for i in v["images"] if key(i) == FRANK_FACE)
        opt = repo.get_option(img["option_id"])
        opt.prompt = "Frank Collins in bed, morning light."
        repo.save_option(opt)
        stored = repo.get_image(img["image_id"])
        stored.prompt, stored.status, stored.file = opt.prompt, "pending", None
        repo.save_image(stored)
        s.wait()
        failed = repo.get_image(img["image_id"])
        assert failed.status == "failed" and REAL_PERSON_REASON in failed.error
