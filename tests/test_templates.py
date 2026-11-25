"""Story templates learned from existing titles (docs/templates.md)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures import sample
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.models import Job, JobRequest
from drama_agent.pipeline.orchestrator import Orchestrator
from drama_agent.store import InMemoryRepository
from drama_agent.templates import (
    TEMPLATE_RULES_VERSION,
    LearnError,
    StoryTemplate,
    copied_specifics,
    fixture_template,
    get_template,
    learn_issues,
    learn_template,
    list_templates,
    save_template,
    template_rules,
)


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "templates"
    monkeypatch.setenv("DRAMA_TEMPLATES_DIR", str(d))
    return d


def learned(**extra) -> StoryTemplate:
    return asyncio.run(learn_template(FixtureModelClient(), "Sample Film", year=2001, **extra))


def with_template(template_id: str, **options) -> JobRequest:
    raw = sample.sample_request().model_dump(mode="json")
    raw["options"] |= {"template_id": template_id, **options}
    return JobRequest.model_validate(raw)


def run(request: JobRequest, client: FixtureModelClient):
    repo = InMemoryRepository()
    repo.save_job(Job(job_id="job_t", request=request))
    return asyncio.run(Orchestrator(repo, client).run_job("job_t")), repo


def test_learning_saves_a_template_that_loads_back(tdir):
    t = learned()
    assert t.template_id == "secret_identity_public_fall"  # from the pattern's name
    assert t.learned_from.title == "Sample Film" and t.learned_from.sources == ["model_knowledge"]
    save_template(t)
    assert get_template(t.template_id) == t
    assert [x.template_id for x in list_templates()] == [t.template_id]
    assert get_template("../etc/passwd") is None and get_template("nope") is None
    # A script given to learn from is read, never stored.
    t2 = learned(script="An unrelated script.", template_id="mine")
    assert t2.learned_from.sources == ["model_knowledge", "local_script"]
    assert "unrelated" not in t2.model_dump_json()


def test_a_template_must_not_copy_its_source():
    body = fixture_template(None)
    assert learn_issues(body, None) == []
    # Naming a source specific in the pattern.
    named = body.model_copy(update={"hook": "Open on Sample Heir being shamed."})
    assert any("Sample Heir" in i.evidence for i in learn_issues(named, None))
    # Eight words in a row from the script it was learned from.
    script = "She told them all that the rival wants what the lead secretly already has."
    assert any("copies the script" in i.evidence for i in learn_issues(body, script))
    # Beats for every episode; a few of the source's own specifics.
    short = body.model_copy(update={"structure": body.structure[:2], "source_specifics": []})
    checks = {i.check for i in learn_issues(short, None)}
    assert checks == {"template_structure", "template_specifics"}


def test_learning_retries_then_gives_up():
    class Copies(FixtureModelClient):
        def __init__(self, clean_on: int | None):
            super().__init__()
            self.clean_on = clean_on

        async def generate(self, request, output_model):
            body = await super().generate(request, output_model)
            if request.attempt != self.clean_on:
                body.hook = "Open on Sample Heir."
            return body

    client = Copies(clean_on=1)
    asyncio.run(learn_template(client, "Sample Film"))
    assert len(client.calls) == 2 and "Sample Heir" in client.calls[1].feedback[0].evidence
    with pytest.raises(LearnError, match="Sample Heir"):
        asyncio.run(learn_template(Copies(clean_on=None), "Sample Film"))


def test_rules_give_each_step_its_part():
    t = learned()
    outline = template_rules(t, "outline")
    assert "the hidden lead" in outline and "Episode 1, 0-5 s" in outline
    assert all(s in outline for s in t.source_specifics)  # the originality rule, every step
    assert "close on reactions" in template_rules(t, "storyboard")
    assert "status shown by tailoring" in template_rules(t, "cast")
    assert "Roles" not in template_rules(t, "storyboard")
    assert copied_specifics(t, "He met SAMPLE heir at the gala.") == ["Sample Heir"]
    assert copied_specifics(t, "Sample Heiress") == []  # whole words only


def test_a_job_follows_its_template(tdir):
    t = learned()
    save_template(t)
    client = FixtureModelClient()
    job, repo = run(with_template(t.template_id), client)
    assert job.status == "completed", job.failure_reason
    outline = next(c for c in client.calls if c.stage == "outline")
    assert f'Story template "{t.name}"' in outline.instructions
    assert "the hidden lead" in outline.instructions
    versions = repo.get_package(job.package_id).provenance.prompt_versions
    assert versions["template_rules"] == TEMPLATE_RULES_VERSION
    assert versions[f"template:{t.template_id}"] == "1"


def test_a_story_using_the_sources_names_is_sent_back(tdir):
    """The sample story has a Frank Collins: as a template's source specific, the outline
    step must replace him, and the fixture never does."""
    t = learned()
    t.source_specifics.append("Frank Collins")
    save_template(t)
    job, _ = run(with_template(t.template_id), FixtureModelClient())
    assert job.status == "needs_revision" and job.stages["outline"].status == "failed"
    [issue, *_] = job.stages["outline"].issues
    assert issue.check == "template_originality" and "Frank Collins" in issue.evidence


def test_templates_api(tdir, tmp_path):
    t = learned()
    save_template(t)
    app = create_app(client=FixtureModelClient(), media=MediaStore(tmp_path / "m"), poll_s=0.01)
    with TestClient(app) as http:
        assert [x["template_id"] for x in http.get("/v1/templates").json()] == [t.template_id]
        assert http.get(f"/v1/templates/{t.template_id}").json()["name"] == t.name
        assert http.get("/v1/templates/nope").status_code == 404
        body = with_template("nope").model_dump(mode="json")
        assert http.post("/v1/drama-jobs", json=body).status_code == 422
        body = with_template(t.template_id, reference_mode="adaptation").model_dump(mode="json")
        assert "inspiration" in http.post("/v1/drama-jobs", json=body).json()["detail"]
        body = with_template(t.template_id).model_dump(mode="json")
        assert http.post("/v1/drama-jobs", json=body).status_code == 202
