# drama-agent

Async backend service that turns a creative seed (a sentence, photo, video, or existing
script) into a text production package for a **3-episode, 120s-per-episode vertical short
drama**: story bible, full scripts, text storyboards, and structured art asset requirements
for downstream design.

Design source: [`drama_agent_service_v0_1.md`](drama_agent_service_v0_1.md) (spec v0.2).

## Quick start

```bash
uv sync
uv run pytest                                   # 28 tests
uv run uvicorn drama_agent.api.app:app --reload # http://127.0.0.1:8000/docs
```

```bash
curl -X POST localhost:8000/v1/drama-jobs -H 'content-type: application/json' \
  -d '{"sources":[{"type":"text","text":"一个即将失去店铺的年轻人，发现前来收租的老人是失踪多年的父亲。","use_as":"story_seed"}]}'
curl localhost:8000/v1/drama-jobs/<job_id>
curl 'localhost:8000/v1/drama-packages/<package_id>?format=markdown'
```

By default the service uses `FixtureModelClient`, which returns a built-in sample package,
so everything runs offline. No real model is wired in yet.

## Layout

```
src/drama_agent/
  models/          Pydantic contracts: request, source brief, story bible, screenplay,
                   storyboard + timing, asset requirements, package/job/revision
  pipeline/
    orchestrator.py  One orchestrator, five gated steps, bounded per-step retries
    steps.py         ingest → outline → script → storyboard → review
    asset_aggregation.py  Derives/merges asset requirements from shots (reuse units,
                          used_by, depends_on are program-owned)
    impact.py        Revision impact analysis and stale-requirement propagation
  validation/      Deterministic hard checks (spec §8) and ID index
  llm/             Vendor-neutral ModelClient protocol + offline fixture client
  prompts/         Base orchestrator prompt (verbatim from spec §10)
  store/           Repository protocol + in-memory implementation
  api/             FastAPI routes (spec §9)
  fixtures/        Complete sample package used by the fixture client and tests
```

## Design choices

- **Program owns invariants, model owns prose.** Timeline totals, ID references, dialogue
  scheduling, asset reuse/merging and dependency graphs are validated or built in code;
  the model only writes creative content.
- **Gated steps with bounded retries.** A failing step retries itself with the issues as
  feedback (default budget 2). If it's still failing, the job ends `needs_revision` with
  the draft kept; it is never marked `completed`.
- **`spoken_timing_verified` is always false** until a voice-measurement step exists; the
  hard checks reject a package that claims otherwise.
- **Revisions are immutable.** Packages are stored per revision; a stale `base_revision_id`
  returns 409.
- `RevisionRequest.visual_change` is an addition to the spec. It says whether an edit to a
  character/location/prop changes how it looks (which invalidates art requirements) or
  only changes story facts.

## Not implemented yet

- A real `ModelClient` (implement `llm/base.py::ModelClient` with structured output).
- Image/video/script input adapters (transcription, keyframes, coverage ranges).
- The revision regeneration worker: `POST .../revisions` validates the request, runs the
  impact analysis and records the revision as `queued`, but nothing regenerates content yet.
- Upstream retry: a failing step only retries itself. It never re-runs earlier steps.
- Persistent storage and a durable job queue (the current version uses in-memory storage
  and FastAPI background tasks).
- Source asset upload endpoint.
