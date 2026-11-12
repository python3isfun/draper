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

- submit a seed or script, and/or upload images and videos (each with an optional
  prompt), then watch the pipeline steps progress
- browse the result: story bible, scripts, the timed storyboard with a shot timeline,
  the cast, set and prop sheets, asset requirements (filterable by type), and QA issues with token usage
- request a targeted revision and see its impact analysis (conflicts, locked objects in
  scope, impacted objects, stale asset requirements)
- export a package as Markdown or JSON

Model output is always inserted as text, never as HTML.

### Live activity, trace and pause

While a job runs, its page shows an **Activity** view (also a tab on finished jobs):

- **Agent steps:** step started/passed, hard-check issues and retries, schema repairs,
  text cleanup, pause/resume, and failures, with timestamps.
- **Model calls:** each request to the model, streaming live: the model's **progress
  notes**, its **reasoning summary**, and the **output** as it's generated. Finished calls
  show model, duration and tokens; their full output and the exact prompt load on demand.

Everything is stored in SQLite (`job_events`, `llm_calls`) for debugging a story later.
API: `GET /v1/drama-jobs/{id}/trace?after_seq=N` (incremental) and
`GET /v1/llm-calls/{call_id}` (one call in full).

Raw model reasoning is encrypted by the provider and isn't available; the reasoning
*summary* and progress notes come from the Responses API (`MCPHUB_API=responses`, the
default). `MCPHUB_API=chat` uses streaming Chat Completions instead: live output, but no
notes or summaries.

**Pause** (`POST /v1/drama-jobs/{id}/pause`) stops a queued job at once, and a running one
once its current model call finishes, so that call's work is kept. **Resume**
(`POST .../resume`) continues from the checkpoint; pausing doesn't use up a retry attempt.

To try the live view offline, run with `DRAMA_MODEL_CLIENT=fixture
DRAMA_FIXTURE_DELAY_S=4`: each sample step then takes about 4 seconds and streams.

### Image and video sources

A job can start from uploaded images and videos, alone or with a text idea. Each file can
carry an optional **prompt** (the source's `text`), which the model treats as your
direction for that file, for example "让她成为女主角，复仇题材". Text that appears inside
an image, or is spoken in a video, is material to analyze, not an instruction.

```bash
# 1. Upload the raw file (not multipart). The type is detected from the bytes.
curl -X POST --data-binary @street.jpg 'http://127.0.0.1:8000/v1/source-assets?filename=street.jpg'
# -> {"asset_id": "asset_...", "kind": "image", ...}

# 2. Use it as a source.
curl -X POST http://127.0.0.1:8000/v1/drama-jobs -H 'content-type: application/json' -d '{
  "sources": [{"type": "image", "asset_id": "asset_...", "text": "让她成为女主角，复仇题材"}]}'
```

- **Images** (JPEG, PNG, WebP, GIF): a copy downscaled to 1536 px goes to the model.
- **Videos** (MP4, MOV, WebM): evenly spaced keyframes are extracted on upload (up to 24,
  each labeled with its timestamp). The audio is transcribed with `whisper-1` at the
  ingest step, and the transcript is cached on the asset. The model records which time
  ranges it actually analyzed. The ingest gate rejects coverage ranges that fall outside
  the video. A failed transcription doesn't fail the job: the audio is recorded as unknown.
- Only the **ingest** step sees the media. Later steps work from the source brief.
- The job page lists the sources, the keyframes the model saw, and the transcript. The
  activity trace records the transcription as its own call. Recorded prompts keep image
  labels and file names, not the image data.

A job takes at most 12 images and videos. HEIC/AVIF photos aren't supported yet (export
as JPEG or PNG). Video needs `ffmpeg` and `ffprobe` on the server; without them, video
uploads are refused and only images up to 5 MB are accepted, since they're sent as uploaded. Endpoints: `GET /v1/source-assets/{id}` (metadata,
keyframes, transcript), `.../content` (the file, supports range requests),
`.../frames/{n}` (a keyframe).

| variable | default | meaning |
| --- | --- | --- |
| `DRAMA_MEDIA_DIR` | `data/media` | where uploads and extracted frames are stored |
| `DRAMA_MAX_IMAGE_MB` / `DRAMA_MAX_VIDEO_MB` | `20` / `500` | upload size limits |
| `DRAMA_MAX_VIDEO_S` | `3600` | longest accepted video |
| `DRAMA_VIDEO_FRAMES` | `24` | keyframes extracted per video |
| `DRAMA_MAX_IMAGES_PER_REQUEST` | `48` | images per model request; videos' keyframes are thinned evenly to fit |
| `DRAMA_TRANSCRIBE_MAX_S` | `1800` | audio after this is not transcribed (marked unknown) |
| `MCPHUB_TRANSCRIBE_MODEL` | `whisper-1` | speech-to-text model |

### Cast, set and prop sheets

Each package includes three standalone design documents. Each one has its own versioned
JSON Schema and is written by its own model call:

| sheet | describes | schema | per package |
| --- | --- | --- | --- |
| `cast_sheet` | every character (age, build, face, hair, distinguishing features, wardrobe and palette, mannerisms, voice) and every look | `/v1/schemas/cast-sheet.json` | `GET /v1/drama-packages/{id}/cast-sheet` |
| `set_sheet` | every location (interior/exterior, era, layout, furnishings, materials, palette, lighting, entrances and blocking, ambient sound) and every location state | `/v1/schemas/set-sheet.json` | `.../set-sheet` |
| `prop_sheet` | every key prop (size, materials, finish, wear, distinguishing marks, handling) and every prop state | `/v1/schemas/prop-sheet.json` | `.../prop-sheet` |

Every entry also lists `must_keep` (what the story depends on) and `design_freedom`
(what a designer may decide). These are text descriptions, not generated images.

- **Story bible vs. sheets.** The story bible keeps the story-facing side: IDs, names,
  roles, goals, relationships, arcs and one-line summaries. The sheets hold how each entity
  looks, keyed by the same IDs.
- **Pipeline.** The `cast`, `sets` and `props` steps run concurrently after the script, so
  descriptions follow how the script uses each entity. Each sheet has its own hard checks
  and retry budget: every story-bible ID is described exactly once, nothing unknown is
  added, variants sit under the right parent, a character marked `on_screen: false` is
  never shown in a shot, and no IDs or timecodes leak into prose. A sheet step doesn't see
  the other sheets; storyboard and review see all three. If one sheet's model call fails,
  the other two still finish and are checkpointed, so a retry only redoes the failed one.
- **Downstream.** Asset requirements take their design brief, `must_keep` and
  `design_freedom` from the sheets. Locking a character, location or prop also locks its
  sheet entry.
- **Older data.** Packages made before the sheets load without them; the sheet endpoints
  return 404 with an explanation. Their old visual fields (appearance, costume, layout,
  ...) are kept under `legacy` on each story-bible entry: still shown in the Overview and
  used for asset briefs, but hidden from the model's schema. Completed jobs show the new steps as `skipped`. A
  retried or resumed old job runs the sheets, then re-runs storyboard and review.

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
- **Manual retry.** `POST /v1/drama-jobs/{job_id}/retry` (the **Retry** button in the web
  UI) re-runs a `needs_revision` or `failed` job from the step that didn't pass, keeping
  the steps that did. The result is saved as a new revision; the failed draft stays
  available under its own revision ID.
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
`MCPHUB_TIMEOUT_S` is optional (default 600).

**Per-step models.** `MCPHUB_MODEL` is the default for every pipeline step. Override it
for individual steps with `MCPHUB_MODEL_INGEST`, `MCPHUB_MODEL_OUTLINE`,
`MCPHUB_MODEL_SCRIPT`, `MCPHUB_MODEL_STORYBOARD` and `MCPHUB_MODEL_REVIEW`, e.g. a cheaper
model for the light steps:

```bash
MCPHUB_MODEL="gpt-6.1-sol"
MCPHUB_MODEL_INGEST="gpt-6-luna"
MCPHUB_MODEL_REVIEW="gpt-6-luna"
```

A misspelt override (e.g. `MCPHUB_MODEL_SCRIPTS`) stops the server at startup instead of
being ignored. The model each step actually used is recorded in
`provenance.model_versions`, and the web UI header lists the active overrides. Having a key set never switches the backend
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
  models/          Pydantic contracts: request, source brief, story bible, design sheets
                   (cast/set/prop), screenplay, storyboard + timing, asset requirements,
                   package/job/revision, uploaded media
  pipeline/
    orchestrator.py  One orchestrator, gated steps (some concurrent), bounded per-step retries
    steps.py         ingest → outline → script → {cast, sets, props} → storyboard → review
    asset_aggregation.py  Derives/merges asset requirements from shots (reuse units,
                          used_by, depends_on are program-owned)
    impact.py        Revision impact analysis and stale-requirement propagation
  validation/      Deterministic hard checks (spec §8) and ID index
  llm/             Vendor-neutral ModelClient protocol, MCPHub client, offline fixture
  config.py        .env loading and model client selection
  prompts/         Base orchestrator prompt (verbatim from spec §10)
  store/           Repository protocol; SQLite (default) and in-memory backends
  jobqueue.py      Durable job queue (leases, retries, checkpoints)
  media.py         Uploaded images/videos: storage, type sniffing, keyframes, audio
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

- Script files as uploads (`type: script` with an `asset_id`); paste scripts as text.
- Non-speech audio analysis (music, sound effects) for videos.
- The revision regeneration worker: `POST .../revisions` validates the request, runs the
  impact analysis and records the revision as `queued`, but nothing regenerates content yet.
- Upstream retry: a failing step only retries itself. It never re-runs earlier steps.
