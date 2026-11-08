# drama-agent

Async backend service that turns a creative seed (a sentence, photo, video, or existing
script) into a text production package for a **3-episode, 120s-per-episode vertical short
drama**: story bible, full scripts, text storyboards, and structured art asset requirements
for downstream design.

Design source: [`drama_agent_service_v0_1.md`](drama_agent_service_v0_1.md) (spec v0.2).

## Quick start

```bash
uv sync
uv run pytest                                   # offline tests; no network
uv run uvicorn drama_agent.api.app:app --reload
                                                # http://127.0.0.1:8000/docs
```

Then open http://127.0.0.1:8000/ for the web UI (the API docs are at `/docs`).

### Web UI

A static single-page app (`src/drama_agent/web/`) served by the same FastAPI process,
with no build step. It lets you:

- submit a seed or script and watch the five pipeline steps progress
- browse the result: story bible, scripts, the timed storyboard with a shot timeline,
  asset requirements (filterable by type), and QA issues with token usage
- request a targeted revision and see its impact analysis (conflicts, locked objects in
  scope, impacted objects, stale asset requirements)
- export a package as Markdown or JSON

Model output is always inserted as text, never as HTML.

### Storage

The server stores everything in SQLite by default, at `data/drama.db` relative to the
directory you start it from (git-ignored). Jobs, packages, revisions and evaluation reports
survive restarts.

| variable | default | meaning |
| --- | --- | --- |
| `DRAMA_STORE` | `sqlite` | `sqlite`, or `memory` for nothing written to disk |
| `DRAMA_DB_PATH` | `data/drama.db` | SQLite file; parent directories are created |

Each object is stored as its JSON document. Package revisions are append-only: one row per
revision, never overwritten.

Tests use the in-memory store unless they pass a temporary SQLite file.

### Job queue

Jobs go through a durable queue kept in the same SQLite file (`job_queue` table), so they
survive restarts and crashes:

- **Leases.** A worker claims a job for `DRAMA_JOB_LEASE_S` seconds (default 30) and renews
  the lease every third of that while the job runs. If the worker dies, the lease expires
  and another worker picks the job up.
- **Checkpoints.** After each pipeline step passes, its output is saved. A resumed job skips
  the steps that already passed, so finished model calls aren't paid for twice.
- **Retries.** A failed job is retried with exponential backoff (5s, 10s, ...) up to
  `DRAMA_JOB_MAX_ATTEMPTS` (default 3). `needs_revision` is a final result and isn't
  retried. While a job waits to retry, its status is `queued` and `failure_reason` says why.
- **Graceful shutdown.** Stopping a worker (Ctrl+C / SIGTERM) hands its job back to the
  queue at once without spending an attempt.

Workers run inside the API process by default (`DRAMA_WORKERS`, default 1). To scale or
isolate them, run the API with no workers and start worker processes against the same
database file:

```bash
DRAMA_WORKERS=0 uv run uvicorn drama_agent.api.app:app
uv run drama-worker                 # or: python -m drama_agent.worker
DRAMA_WORKERS=4 uv run drama-worker # four concurrent jobs in one process
```

With `DRAMA_STORE=memory` the queue is in memory too: same behaviour, nothing survives a
restart, and separate worker processes aren't possible.

### Model backend

`DRAMA_MODEL_CLIENT` picks the backend (read from the environment or `.env`):

| value | backend |
| --- | --- |
| `fixture` (default) | Built-in sample package, offline, free |
| `mcphub` | [MCPHub](https://api.mcphub.com) OpenAI-compatible gateway |

`mcphub` needs `MCPHUB_URL`, `MCPHUB_KEY` and `MCPHUB_MODEL` (e.g. `gpt-6.1-sol`);
`MCPHUB_TIMEOUT_S` is optional (default 600). Having a key set never switches the backend
on its own; set `DRAMA_MODEL_CLIENT=mcphub` explicitly. `.env` is git-ignored.

```bash
DRAMA_MODEL_CLIENT=mcphub uv run uvicorn drama_agent.api.app:app
RUN_LIVE=1 uv run pytest -m live                # one real MCPHub call
```

The client requests JSON-schema structured output in non-strict mode (the models contain
open maps that strict mode rejects), validates locally, and gives the model one repair turn
on validation errors. Token usage per stage is recorded in `provenance.token_usage`.

```bash
curl -X POST localhost:8000/v1/drama-jobs -H 'content-type: application/json' \
  -d '{"sources":[{"type":"text","text":"一个即将失去店铺的年轻人，发现前来收租的老人是失踪多年的父亲。","use_as":"story_seed"}]}'
curl localhost:8000/v1/drama-jobs/<job_id>
curl 'localhost:8000/v1/drama-packages/<package_id>?format=markdown'
```

By default the service uses `FixtureModelClient`, which returns a built-in sample package,
so everything runs offline.

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
  llm/             Vendor-neutral ModelClient protocol, MCPHub client, offline fixture
  config.py        .env loading and model client selection
  prompts/         Base orchestrator prompt (verbatim from spec §10)
  store/           Repository protocol; SQLite (default) and in-memory backends
  jobqueue.py      Durable job queue (leases, retries, checkpoints)
  worker.py        Job workers; `drama-worker` entry point
  api/             FastAPI routes (spec §9) + /v1/meta, job listing
  web/             Web UI (HTML/CSS/JS, served at /)
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

- Image/video/script input adapters (transcription, keyframes, coverage ranges).
- The revision regeneration worker: `POST .../revisions` validates the request, runs the
  impact analysis and records the revision as `queued`, but nothing regenerates content yet.
- Upstream retry: a failing step only retries itself. It never re-runs earlier steps.
- Source asset upload endpoint.
