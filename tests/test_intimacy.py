"""Intimacy: scene staging within the content rating, adults only, the export's intimacy
plan, and the content guard on image prompts."""

import zipfile

import pytest
from fastapi.testclient import TestClient

from drama_agent.content_guard import (
    EXPLICIT_REASON,
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
    assert intimacy_issues(package, "PG-13") == []
    for rating in ("G", "PG"):
        found = intimacy_issues(package, rating)
        assert len(found) == 1 and "at most kiss" in found[0].evidence
        assert found[0].object_id == sc.scene_id and found[0].fix_scope == "script"

    sc.intimacy.level = "simulated"
    assert any("at most implied" in i.evidence for i in intimacy_issues(package, "PG-13"))
    r = intimacy_issues(package, "R")
    assert [i.evidence for i in r] == ["simulated intimacy or nudity needs closed_set=true"]
    sc.intimacy.closed_set = True
    assert intimacy_issues(package, "R") == []
    # Jobs stored before the MPA ratings: general, teen and mature read as PG, PG-13, R.
    assert intimacy_issues(package, "mature") == []
    assert JobOptions(content_rating="teen").content_rating == "PG-13"
    assert JobOptions(content_rating="general").content_rating == "PG"
    assert JobOptions(content_rating="Teen").content_rating == "PG-13"
    assert JobOptions(content_rating="pg-13").content_rating == "PG-13"
    with pytest.raises(ValueError):
        JobOptions(content_rating="NC-17")
    from drama_agent.models.request import as_rating

    assert as_rating("r") == "R" and as_rating(" pg ") == "PG" and as_rating("nope") == "PG-13"


def test_nudity_consent_and_staging_are_required(package):
    sc, _ = stage(package, nudity="partial", consent_beats=[], on_screen=" ")
    found = " | ".join(i.evidence for i in intimacy_issues(package, "PG-13"))
    assert "partial nudity, which the PG-13 rating does not allow" in found
    assert "no consent_beats" in found
    assert "on_screen is empty" in found
    assert "needs closed_set=true" in found
    sc.intimacy.character_ids = ["ch_nobody"]
    assert any("ch_nobody" in i.evidence for i in intimacy_issues(package, "PG-13"))


def test_default_rating_and_untouched_packages_pass(package):
    assert JobOptions().content_rating == "PG-13"
    assert intimacy_issues(package, "G") == []  # no scene has intimacy
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
        ("A sexually explicit scene", EXPLICIT_REASON),
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
        # A bare shoulder, arm or foot is not nudity; the person being naked still is.
        ("Evening makeup, shirt stripped with naked shoulder exposed.", None),
        ("Naked feet on the cold floor, nude arms folded.", None),
        ("Shirt off her naked left shoulder, then she is naked.", NUDITY_REASON),
        # A harmless part first doesn't clear what follows it.
        ("A naked arms and chest portrait.", NUDITY_REASON),
        ("Naked legs, torso and back.", NUDITY_REASON),
        ("A naked back and naked breasts", NUDITY_REASON),
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
            return output_model(
                allowed=False, real_person=True, refusal="Not with an uploaded photo."
            )
        if request.stage == "content_guard":
            return output_model(
                allowed=False, real_person=True, reason="Not with an uploaded photo."
            )
        return await super().generate(request, output_model)


def test_experiment_writer_refusal_draws_nothing(tmp_path):
    with Chat(tmp_path, text=Refuser()) as c:
        c.send(attachments=[c.upload()])
        c.send(text="put her in a bikini on a bed")
        turn = c.wait()["turns"][-1]
        assert turn["status"] == "failed" and turn["refused"]
        assert turn["error"] == "Not with an uploaded photo."
        assert c.images.calls == [] and turn["prompt"] == ""
        # "Check again" runs the check under the current rules; still refused here.
        r = c.http.post(f"/v1/experiment-turns/{turn['turn_id']}/retry")
        assert r.status_code == 202
        again = c.wait()["turns"][-1]
        assert again["refused"] and c.images.calls == []


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


@pytest.mark.parametrize(
    ("prompt", "allowed"),
    [
        # Clothed romance and swimwear: every rating.
        ("Two adults kiss at a bus stop.", "G PG PG-13 R"),
        ("A woman in a red bikini on the beach.", "G PG PG-13 R"),
        ("Two lovers holding hands in the park.", "G PG PG-13 R"),
        # Suggestive, no nudity: PG-13 and up.
        ("She stands in black lace lingerie by the window.", "PG-13 R"),
        ("A bed scene, the couple under the covers, shoulders bare.", "PG-13 R"),
        # Nudity: R only.
        ("A naked woman under a sheet in the morning light.", "R"),
        ("She is topless, seen from behind.", "R"),
        # Never, at any rating.
        ("A sexually explicit close-up.", ""),
        ("Two adults having sex.", ""),
        ("Her genitals are visible.", ""),
        # Minors: never romantic or bare, at any rating.
        ("A naked teenager on the beach.", ""),
        ("A 16-year-old in lingerie.", ""),
    ],
)
def test_text_guard_follows_the_rating(prompt, allowed):
    for rating in ("G", "PG", "PG-13", "R"):
        assert (text_guard(prompt, rating) is None) == (rating in allowed.split()), rating


def test_rules_follow_the_rating():
    from drama_agent.content_guard import rules

    assert "rated G" in rules("G") and "nothing suggestive" in rules("G")
    assert "rated PG-13" in rules() and "lingerie" in rules("PG-13")
    assert "nudity of adult characters is allowed" in rules("R")
    assert "rated R" in rules("mature")  # a legacy rating reads as its MPA one
    for rating in ("G", "PG", "PG-13", "R"):  # rules 2 and 3 never change
        assert "anyone who is or looks under 18" in rules(rating)
        assert "Never, at any rating: a sexualized image of a real person's likeness" in rules(
            rating
        )


def test_upload_guard():
    assert upload_guard("Lila in a black lace bra and panties") == REAL_PERSON_REASON
    assert upload_guard("Two people in bed, morning light") == REAL_PERSON_REASON
    assert upload_guard("A nude portrait") == NUDITY_REASON
    # R allows nudity, but never of someone who may be a real person.
    assert upload_guard("A nude portrait", rating="R") == REAL_PERSON_REASON
    assert upload_guard("Lila in a lace bra by the window", rating="R") == REAL_PERSON_REASON
    assert upload_guard("Lila in a charcoal blazer, nude lipstick") is None
    assert text_guard("Lila in a red bikini on the beach") is None  # fine without uploads


class UploadSession(Session):
    """A design-board session whose job has an uploaded photo (a character reference)."""

    use_as = "character_reference"

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        asset = self.http.post(
            "/v1/source-assets", params={"filename": "me.png"}, content=PORTRAIT
        ).json()
        body = {
            "sources": [
                {"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"},
                {"type": "image", "asset_id": asset["asset_id"], "use_as": self.use_as},
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
        r = s.http.post(url, json={"prompt": "Frank Collins in his underwear."})
        assert r.status_code == 422 and r.json()["detail"] == REAL_PERSON_REASON
        # The design board always uses the restrictive rules: no swimwear either.
        r = s.http.post(url, json={"prompt": "Frank Collins in swimwear on a beach."})
        assert r.status_code == 422 and r.json()["detail"] == REAL_PERSON_REASON
        r = s.http.post(url, json={"prompt": "Frank Collins in a wool coat, side view."})
        assert r.status_code == 202
        screened = [c for c in s.text.calls if c.stage == "content_guard"][-1]
        assert "Restrictive mode:" in screened.instructions
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


# ---------------------------------------------------------------------------
# Real-person likeness and Restrictive mode (Experiment)


class Judge(FixtureModelClient):
    """A writer that judges likeness like the real one: messages with "like <name>" make a
    real person's likeness; it may also choose pictures."""

    def __init__(self, pictures=None):
        super().__init__()
        self.pictures = pictures

    async def generate(self, request, output_model):
        if request.stage == "experiment":
            self.calls.append(request)
            request.model = "judge-v1"  # as the real client does
            ctx = request.context
            n = len(ctx["pictures"] if self.pictures is None else self.pictures)
            defs = " ".join(f"<Picture {i}> is <Subject {i}>." for i in range(1, n + 1))
            return output_model(
                real_person="look like" in ctx["message"],
                pictures=self.pictures,
                prompt=f"{defs} The target image ... No other lettering is added."
                if n
                else "A photograph. The image contains no text, letters or numbers.",
            )
        return await super().generate(request, output_model)


def writer_calls(c):
    return [r for r in c.text.calls if r.stage == "experiment"]


def test_a_clothing_reference_passes_on_no_likeness(tmp_path):
    with Chat(tmp_path, text=Judge()) as c:
        c.send(text="a woman in a blazer")
        c.wait()
        c.send(text="put her in the cardigan from the photo", attachments=[c.upload()])
        turn = c.wait()["turns"][-1]
        assert turn["from_upload"] and turn["likeness"] is False
        c.send(text="a man hugs her from behind")
        c.wait()
        kind = writer_calls(c)[-1].context["pictures"][0]["kind"]
        assert kind == "made in this chat; no real person's likeness (fictional)"


def test_a_named_likeness_is_inherited_until_you_go_back(tmp_path):
    with Chat(tmp_path, text=Judge()) as c:
        c.send(text="a woman in a blazer")  # #1
        c.wait()
        c.send(text="make her look like a famous actress")  # #2
        assert c.wait()["turns"][-1]["likeness"] is True
        c.send(text="show the front view")  # #3: the writer says no, Picture 1 says yes
        turn = c.wait()["turns"][-1]
        assert turn["likeness"] is True
        assert (
            writer_calls(c)[-1]
            .context["pictures"][0]["kind"]
            .endswith("shows a real person's likeness")
        )
    with Chat(tmp_path, text=Judge(pictures=["#1"])) as c:
        c.send(text="a woman in a blazer")  # #1
        c.wait()
        c.send(text="make her look like a famous actress")  # #2 (the writer sends #1 only)
        c.wait()
        c.send(text="go back to #1 and add a scarf")
        assert c.wait()["turns"][-1]["likeness"] is False  # built on #1, which has none


def test_dropping_pictures_keeps_the_likeness(tmp_path):
    with Chat(tmp_path, text=Judge()) as c:
        c.send(text="make her look like a famous actress")
        assert c.wait()["turns"][-1]["likeness"] is True
    with Chat(tmp_path, text=Judge(pictures=[])) as c:
        c.send(text="a woman")
        c.wait()
        repo = c.app.state.repo
        [t] = repo.list_exp_turns(c.sid)
        t.likeness = True  # as if she had been made to look like someone
        repo.save_exp_turn(t)
        c.send(text="draw her again from scratch")  # no picture sent, but she was seen
        turn = c.wait()["turns"][-1]
        assert turn["references"] == [] and turn["likeness"] is True


def test_older_results_are_judged_by_the_model(tmp_path):
    old = ExpTurn(turn_id="turn_old", session_id="s", index=1, status="done")
    assert old.likeness is None
    kind = picture_kind("turn:turn_old:0", [old])
    assert kind.startswith("made in this chat before likeness was tracked")
    assert picture_kind("asset:asset_x", []).startswith("uploaded photo (judge")


def test_restrictive_mode_reaches_the_writer_and_the_screen(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="a woman in a blazer")
        turn = c.wait()["turns"][-1]
        assert turn["restrictive"] is False
        assert "Open mode" in writer_calls(c)[-1].instructions
        c.send(text="a man in a coat", restrictive=True)
        assert c.wait()["turns"][-1]["restrictive"] is True
        assert "Restrictive mode:" in writer_calls(c)[-1].instructions
        assert "Open mode" not in writer_calls(c)[-1].instructions
        c.send(text="A photograph of a red door.", mode="exact", restrictive=True)
        c.wait()
        screened = [r for r in c.text.calls if r.stage == "content_guard"][-1]
        assert "Restrictive mode:" in screened.instructions


class SeesARealPerson(FixtureModelClient):
    async def generate(self, request, output_model):
        if request.stage == "content_guard":
            self.calls.append(request)
            return output_model(allowed=True, real_person=True)
        return await super().generate(request, output_model)


def test_exact_prompts_get_the_likeness_from_the_screen(tmp_path):
    with Chat(tmp_path, text=SeesARealPerson()) as c:
        c.send(text="A portrait in the style of a famous actress.", mode="exact")
        assert c.wait()["turns"][-1]["likeness"] is True


class LocationUploadSession(UploadSession):
    use_as = "location_reference"


def test_location_uploads_carry_no_likeness_on_the_design_board(tmp_path):
    with LocationUploadSession(tmp_path) as s:
        s.http.post(
            f"{s.base}/explore", json={**s.ids(), "takes": 1, "draws": 1, "scope": ["cast"]}
        )
        v = s.wait()
        face = next(o for o in v["options"] if key(o) == FRANK_FACE)
        r = s.http.post(
            f"/v1/image-options/{face['option_id']}/edit",
            json={"prompt": "Frank Collins in swimwear on a beach."},
        )
        assert r.status_code == 202, r.text


class Silent(FixtureModelClient):
    """Answers without real_person, as a model that ignores part of the schema would."""

    async def generate(self, request, output_model):
        if request.stage == "experiment":
            self.calls.append(request)
            return output_model.model_validate({"prompt": "A photograph of a woman."})
        if request.stage == "content_guard":
            self.calls.append(request)
            return output_model.model_validate({"allowed": True})
        return await super().generate(request, output_model)


def test_an_unanswered_likeness_check_draws_nothing(tmp_path):
    with Chat(tmp_path, text=Silent()) as c:
        c.send(text="make her look like a famous actress")
        turn = c.wait()["turns"][-1]
        assert turn["refused"] and "Couldn't run the content check" in turn["error"]
        assert c.images.calls == []


def test_older_images_come_with_their_ancestry(tmp_path):
    with Chat(tmp_path) as c:
        c.send(text="make her look like a famous actress")
        c.wait()
        for msg in ["front view", "in a kitchen", "drinking coffee"]:
            c.send(text=msg)
            c.wait()
        repo = c.app.state.repo
        for t in repo.list_exp_turns(c.sid):  # as made before likeness was tracked
            t.likeness = None
            repo.save_exp_turn(t)
        c.send(text="a man hugs her")
        c.wait()
        part = writer_calls(c)[-1].context["pictures"][0]
        assert [a["message"] for a in part["ancestry"]] == [
            "make her look like a famous actress",
            "front view",
            "in a kitchen",
            "drinking coffee",
        ]


def test_open_mode_allows_clothed_romance_with_a_likeness():
    from drama_agent.content_guard import RULES, mode_rule

    assert "clothed romantic scenes (a kiss, an embrace, swimwear" in mode_rule(False)
    assert "no kiss or embrace, no swimwear" in mode_rule(True)
    assert "Never, at any rating: a sexualized image of a real person's likeness" in RULES
    assert upload_guard("Lila in a red swimsuit on a beach", restrictive=False) is None
    assert upload_guard("Lila in a red swimsuit on a beach") == REAL_PERSON_REASON
    assert upload_guard("Lila undressing", restrictive=False) == REAL_PERSON_REASON
