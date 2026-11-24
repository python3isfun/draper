# drama-agent

Async backend service that turns a creative seed (a sentence, photo, video, or existing
script) into a text production package for a **3-episode, 120s-per-episode vertical short
drama**: story bible, full scripts, text storyboards, and structured art asset requirements
for downstream design.

Design source: [`drama_agent_service_v0_1.md`](drama_agent_service_v0_1.md) (spec v0.2). The
spec is written in Chinese; the app itself is English-only.

**Language and setting.** Stories are written in American English and set in the present-day
United States, with American names, places and everyday details. Sources in another language
or setting are adapted. `options.language` must be `en-US` (or left out); the API refuses
anything else. Stories created before this change in Chinese are **archived** at startup:
hidden from the job list (`GET /v1/drama-jobs?include_archived=true` lists them), never run
again, and not rendered in the UI. Their data stays in the database.

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
  prompt), on the **New job** page, then watch the pipeline steps progress. The sidebar
  shows only the current job (or the last one open), with **New job** and **All jobs** (the
  list of every job, also what opens with no job) as pages of their own
- browse the result: story bible, scripts, the timed storyboard with a shot timeline,
  the cast, set and prop sheets, asset requirements (filterable by type), and QA issues with token usage
- revise a published story (the Revise tab): name what to change, how, and what is
  locked. Only the steps that own the targets re-run; everything outside the impacted
  objects, and everything locked, is put back by the program. A result that passes the
  checks is published as a new revision. The story's pictures, keyframes, cut plans and
  readings move to it, and what the revision changed is marked out of date
  (`pipeline/revise.py`; `GET /v1/revisions/{id}` follows a run)
- bring a story up to date in one go (**Update all** in the job's header,
  `web/update.js`): a window first lists what would be done and waits for a confirmation,
  then runs the steps in order with their progress (each busy shot listed with its cut, round, stage such as "drawing 2 of 3" or "checking 1 of 3", and how long it has run): redraw the out-of-date design
  pictures and wait for the image queue; re-plan the out-of-date cut plans the model wrote
  (each keeps its own pace and cut count; plans you wrote are left to you); draw every
  keyframe that can be drawn without an approval (first keyframes, the next cut after an
  approved one, out-of-date keyframes). It never approves anything by itself: the cuts
  waiting for you are approved in the window (from the first look, while keyframes are
  drawn and in the report), each with the draws of its newest run, their checks, a
  full-size view and **Details** (the shot's page, for every take, prompt and check).
  Approving starts the next cut, which comes back once drawn; an approval can be
  withdrawn. The report also lists the shots waiting for design picks and what failed.
  Closing the window keeps it running; reloading the page stops the steps not started yet
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
once its current model call finishes, so that call's work is kept. **Stop now**
(`?now=true`) interrupts a running job immediately, cancelling the in-flight call (for a
call that hangs); the interrupted step restarts on resume. Every model call also has an
overall deadline (`MCPHUB_CALL_TIMEOUT_S`, default 1200 s; storyboard calls take 5-8 min):
MCPHub keeps a stalled stream open with keep-alive lines, which reset httpx's read timeout,
so without it a stalled call would wait forever. A call past the deadline is retried. The deadline is
measured on the wall clock, because asyncio's clock stops while a Mac sleeps; if a call
notices the machine slept (its connection is then presumed dead), it restarts at once.
To stop idle sleep during long runs, start the server under `caffeinate -i`. **Resume**
(`POST .../resume`) continues from the checkpoint; pausing doesn't use up a retry attempt.

To try the live view offline, run with `DRAMA_MODEL_CLIENT=fixture
DRAMA_FIXTURE_DELAY_S=4`: each sample step then takes about 4 seconds and streams.

### Image and video sources

A job can start from uploaded images and videos, alone or with a text idea. Each file can
carry an optional **prompt** (the source's `text`), which the model treats as your
direction for that file, for example "make her the lead in a revenge story". Text that appears inside
an image, or is spoken in a video, is material to analyze, not an instruction.

```bash
# 1. Upload the raw file (not multipart). The type is detected from the bytes.
curl -X POST --data-binary @street.jpg 'http://127.0.0.1:8000/v1/source-assets?filename=street.jpg'
# -> {"asset_id": "asset_...", "kind": "image", ...}

# 2. Use it as a source.
curl -X POST http://127.0.0.1:8000/v1/drama-jobs -H 'content-type: application/json' -d '{
  "sources": [{"type": "image", "asset_id": "asset_...", "text": "make her the lead in a revenge story"}]}'
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

### Adapting an existing script

Set `options.reference_mode` to `adaptation` (Options → Reference mode) to adapt a source
instead of using it as inspiration. Every step then gets the adaptation rules: keep the
source's characters, order of events, reversals, jokes, callbacks and tone; reuse its
dialogue, trimming rather than rewriting; cut and merge to fit 3 x 120 s rather than invent
plot; resolve the source's own contradictions by the majority reading; and say in notes what
was cut. Each source's `must_keep` items are requirements: the ingest step must list each
one word for word under `user_constraints`, or it is retried with the missing items as
feedback. `may_change` lists what may be altered. Packages record
`provenance.prompt_versions.adaptation_rules`.

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
- **Editing by hand.** Each character, look, set, state and item on the **Characters** and
  **Sets** pages has an **Edit** button (the story's Cast, Sets and Props tabs were folded
  into those pages; see "The asset library" below). You
  can edit every sheet field and a look's or state's one-line story description;
  several editors can be open at once, and typed text is kept until it is saved or
  cancelled. `PATCH /v1/drama-packages/{id}/entries/{entry_id}` with `revision_id`,
  `sheet` (fields; lists as lists of strings) and `bible` (`description`). The edit is
  saved **in place** on that revision, with a `change_log` entry. The design text (brief,
  `must_keep`, `design_freedom`) of the entry's asset requirements is rebuilt from the
  sheets, so new takes and Develop use it. If a requirement's text had been written by the
  model instead, the rebuild replaces it and the response says which (`replaced_written`).
  Images already drawn stay as they are, and the response lists them (`drawn_before`).
  IDs and names can't be edited. The edit is refused (409) if the entry, its parent, or a
  requirement built from it is locked, or while a job is still running on the story.
- **What an edit makes out of date, and redrawing it in order.** A picked image is out of
  date when the design text it was drawn from changed (it records that text in its `brief`),
  or a picture it was drawn from was replaced or is out of date itself (a set's other
  states and plan views are drawn from its first wide view; a look from the face). The
  Characters and Sets pages mark each one ("out of date", `outdated` on `GET …/images`) and **Redraw
  out-of-date images** (`POST /v1/drama-packages/{id}/images/refresh`) redraws those whose
  pictures are up to date; each redraw replaces its pick when done, and the images drawn
  from it are redrawn next, so a redesigned set goes wide view, then its other states and
  plan views. A hand-written prompt is kept; a model-written one is rewritten when the text
  changed. A take you write after the edit records the text as it is now. An approved
  **keyframe** is out of date when the set, look or prop text it was written against
  changed, a design picture it was drawn from was replaced or is out of date, or the
  keyframe it continues from changed. The Keyframes tab marks it; **Redraw out-of-date
  keyframes** (`{"outdated": true}` on the batch) starts a take for each shot's first such
  cut once its design pictures are redrawn, and approving it starts the next cut. Until then
  the export's `shot_specs.json` lists the shot as not ready ("[Shot N]'s keyframe is out of
  date: …"). A look edit also makes the scene's reading stale, so it is read again.
  An out-of-date keyframe is never drawn from: a cut whose cut before is out of date waits
  for it, the previous shot's keyframe is left out of a new take until it is redrawn, and
  the batch skips such a shot, naming the one to redraw and approve first. A **cut plan**
  records the design text it was planned against: after an edit, the Keyframes tab says
  "This plan was written before the set, look or prop text changed" (`plan.outdated`
  on the board), since its framing may describe the old design (e.g. "the window behind
  them"); plan the cuts again if it does. Plans and keyframes from before this was recorded
  count as out of date when a hand edit touched their set, looks or props. The keyframe
  writer, its judge and the cut planner read the set sheet (`scene.set`: layout,
  furnishings, materials, the state's light and dressing), so each camera direction puts
  the right wall, window or door behind each person, and the judge fails one that doesn't.

### The asset library (characters and sets)

Draper keeps a library of **characters** (with their looks and the items they carry) and
**sets** (with their states and their dressing), shared across stories. See
`docs/entity-reorganization.md`. So far (phase 1), each story's latest revision is mirrored
into it at startup and before every library read. Pictures stay where they are and are
linked to the asset they show, and each story's casting maps its ids to the library's.
Read it with:

- `GET /v1/library/characters` and `GET /v1/library/sets` (each with a cover picture);
- `GET /v1/library/characters/{id}` and `GET /v1/library/sets/{id}` (looks or states, items,
  current pictures, and the stories and shots that use each);
- `GET /v1/drama-packages/{id}/casting`;
- `POST /v1/library/sync` (`force=true` to rewrite everything).

In the app, **Characters** and **Sets** in the top bar open the library:
- a grid of every character or set, each with its cover, and a filterable list beside it;
- one character or set with its sheet, its looks or states, and its items. Each has its
  current pictures (click to enlarge; out-of-date ones are marked), its one-line
  description, its sheet, and the stories and shots that use it, with links into them.

Each face, look, state and item also has **Takes and draws**: its takes and draws (pick
one), a new take with a direction, and each take's prompt to edit and draw again. The
story's **Casting** tab replaced its Cast, Sets, Props and Assets tabs: it holds the
story-wide Explore and Develop controls, the style brief, and the story's characters, sets
and props, each linking to its library page (old links to those tabs land on Casting).

**Casting across stories.** On a story's Casting tab, each character or set card offers
"Same as <name> in <story>?" when another story has one with the same name (or one name
containing the other, or the same first name). **Link** makes this story use that asset:
its own copy is hidden, and its looks or states join the shared asset, still used by its
own shots and drawn in its own story. Its pictures stay visible under "Also drawn in".
A linked card also has **Use its face** (or **Use its set view**): the shared asset's
picked face, or its set's main wide view, is copied into this story as its pick. What this
story drew from its own (its looks, its other states and plan views) goes out of date, and
"Redraw out-of-date" on the shared page redraws it from the shared picture. The old
pictures stay among the story's takes. API: `POST …/casting/{story_id}/reuse`.
**Unlink** undoes the link. API: `GET /v1/drama-packages/{id}/casting/matches`,
`POST /v1/drama-packages/{id}/casting` (`story_id`, `asset_id`),
`DELETE /v1/drama-packages/{id}/casting/{story_id}`. Archived stories' assets are kept but
not shown or offered.

Each part has an **Edit** button. Your edit is saved to the story the asset came from (the
same edit the story's sheets take), and the library follows. **Redraw out-of-date** redraws
just this character's or set's out-of-date pictures, then what is drawn from them
(`POST …/images/refresh` takes `requirement_ids` to limit it).

### Reference images

On a finished package, **Generate images** (Casting tab, or `POST
/v1/drama-packages/{id}/images`) makes reference images with **MiniMax-H3-Image** on
MCPHub's GPU route, using the same `MCPHUB_KEY`:

- **What gets an image:** one per required deliverable of each character identity (front
  face), look (full body), location state (wide view and top-down plan) and prop state
  (detail). Shot composition references are skipped. A sample-size package has about 20.
- **Consistency:** a look uses the character's face image as a reference, so the same
  person appears in every look. A location's top-down plan uses its wide view, and its other
  states use the first state's wide view. A prop's other states use its first state's
  image. Uploaded photos cited in a requirement's `source_refs` are added as references too.
- **Prompts.** MCPHub's GPU server renders a prompt exactly as written: it doesn't expand it
  first the way MiniMax's own API does. So the app writes the finished prompt. The planner makes
  a structured brief per image: what it is, new image or edit, aspect ratio, each reference
  picture's role, the sheet facts, must-keep items and style. Before the first image of a
  batch, one text-model call (`MCPHUB_MODEL_IMAGE_PROMPTS`, else `MCPHUB_MODEL`) turns every
  pending brief into an English prompt. Its instructions are
  `src/drama_agent/prompts/h3_image_rules.md` and `h3_image_examples.md`, sent verbatim. These
  are the same files the `minimax-h3-image-prompt` Claude Code skill uses, so change the rules
  there. They're based on MiniMax's own prompt-writing skill (sources in the skill's
  `references/sources.md`):
  - say what the image is, then the layout, then visible style and light;
  - quote any lettering, or end with "The image contains no text, letters or numbers.";
  - write edits in MiniMax's form: each `<Picture N>` is defined as a `<Subject N>`, a
    "The target image …" overview, a relationship such as "partially preserved", then the
    changes;
  - give exact counts.

  A written prompt that fails the program's checks falls back to the built-in template, which
  follows the same rules. The checks cover: picture citations, a subject definition for each
  picture, the target overview and the text sentence. A failed call also falls back to the
  template. Regenerate reuses the written prompt with a new seed. Each image shows its prompt
  and who wrote it, and the writer's call appears in the job's Activity view.
- **Running it:** images are generated in the background one at a time, as the GPU server
  works, at about 4 MP (1728x2304 faces, 1536x2720 looks, 2304x1728 sets, 2048x2048 plans
  and props; images made before keep their size). `draft=true` makes
  about 1024 px images, much faster. Progress is live on the Casting tab and the Characters and Sets pages; interrupted images
  resume after a restart. Images never block or fail a job.
- **Versions:** Regenerate (`POST /v1/generated-images/{id}/regenerate`) makes a new
  version with a fresh seed and keeps the old one. A failed image is retried by the next
  Generate. If a reference failed, the image is made without it and says so.
- **Storage:** images are stored under `DRAMA_MEDIA_DIR` as JPEG plus a 512 px thumbnail, as
  records separate from the package (a revision is immutable), keyed by `requirement_id`
  and `deliverable_id` like `fulfilled_by`. `GET /v1/drama-packages/{id}/images` lists them,
  and `GET /v1/generated-images/{id}/content` or `/thumb` serves them.

| variable | default | meaning |
| --- | --- | --- |
| `DRAMA_IMAGE_CLIENT` | follows `DRAMA_MODEL_CLIENT` | `minimax` (real) or `fixture` (local placeholder images) |
| `MCPHUB_IMAGE_URL` | `https://api.mcphub.com/api/v3/gpu-images` | image route (note: not under `/v1`) |
| `MCPHUB_IMAGE_MODEL` | `minimax-h3-image` | image model |
| `GEMINI_API_KEY` / `GEMINI_URL` | — / `https://generativelanguage.googleapis.com/v1beta` | enables Nano Banana (`nano-banana`) as a second provider for keyframes, chat messages and study tests; a gateway serving the Gemini API can stand in for the URL. With fixture images, a local stand-in is offered instead |
| `GEMINI_IMAGE_MODEL` | `gemini-nano-banana-2.1` | the Gemini image model for keyframes, chats and studies |
| `DRAMA_LOCAL_IMAGE_URL` | `http://localhost:8080` | a simple image server of your own that Face Lab tests can be drawn with (`local`): `POST /image` with a `prompt` form field (plus `size` and `seed`, which it may ignore), answering with a PNG or JPEG. With fixture images, a local stand-in |
| `DRAMA_KEYFRAME_PROVIDER` | `h3` | the keyframe provider when a request names none (`h3` or `nano-banana`) |

The image route needs an MCPHub key from a `newsbreak.com` account; it is sent as
`X-API-Key`.

### Casting & design: takes, draws and picks

The story's **Casting** tab (Explore and Develop, story-wide) and each character's and set's
library page (**Takes and draws** per face, look, state and item: its takes, draws, picks
and new takes) together make the **Casting & design** board, which explores several designs per item before
anything else is drawn, so the team chooses from options instead of accepting one image:

1. **Explore** (`POST /v1/drama-packages/{id}/images/explore`, default 4 takes x 3 draws,
   draft size): for every *anchor* (each character's face, each location's first state and
   each prop's first state), one text-model call writes K distinct **takes**. Each take has
   a label, a one-sentence profile and its own H3 prompt. Takes vary only what the design
   sheets leave open; they never change stated facts, must-keep items, or any age,
   gender or heritage the story gives. Each take is drawn N times (different seeds).
2. **Pick** (`POST /v1/generated-images/{id}/select`): one image per item. A draft pick
   is re-rendered at full size (~4 MP) as an *edit of the draft* ("fully preserved"),
   which keeps the face; re-running the prompt at a new size drew a different person. The
   full-size image takes over the pick when it's done (if you haven't picked another
   image meanwhile). Picking that draft again later picks its full-size render; if the
   render failed, picking the draft again queues a new one.
3. **Develop** (`.../images/develop`): every other item is drawn from one **picked**
   parent image, referenced as `<Picture 1>`, with the picked take's label and profile in
   the brief: a look from the face, another location or prop state from the first state,
   and a location state's plan view from that state's wide view. Items whose parent isn't
   picked yet are listed and left for later (run Develop again after picking); New take
   on such an item is refused until then.
4. Refine any item: **+ draws** (`POST /v1/image-options/{id}/draws`) for more of a take,
   **New take** with your direction (`.../images/takes`, e.g. "older, kinder eyes"); the
   writer is told which takes exist so the new one differs.
   A failed draw has **Retry** (`POST /v1/generated-images/{id}/retry`: same prompt and
   seed).
5. Try your own wording: open a take's **Prompt**, edit it and click **Draw edited take**
   (`POST /v1/image-options/{id}/edit`). That makes a new take next to the original with
   the same references and the same seeds as its draws, so draw N of each differs only by
   your change. The prompt goes to the image model as written (no text-model rewrite). A
   prompt citing a `<Picture N>` the take doesn't have is refused; other H3 rule misses
   (e.g. no "The image contains no text..." sentence) are shown as warnings.
6. Use an Experiment image instead (`POST /v1/drama-packages/{id}/images/import` with
   `session_id`, `image` (its #N), `requirement_id`, optional `deliverable_id`): it
   becomes the item's pick, at full size, and Develop draws its looks or states from it.
   It keeps the Experiment's real-person likeness mark (`likeness`; pass `false` for an
   invented person whose image only inherited the mark from someone else in its source).
   Images drawn from a likeness pick carry the mark and get the uploaded-photo rules
   (no underwear, undressing, bed scenes or swimwear), and a likeness face's looks worn
   only in implied or simulated intimate scenes are not drawn (Develop says which).
7. Remove what you don't want: `DELETE /v1/image-options/{id}` deletes a take with all
   its draws, and `DELETE /v1/generated-images/{id}` deletes one image (with its
   full-size render). Both remove the files, so nothing deleted reaches the export, and
   refuse (409) while one of those images is still being drawn.

Takes are stored as `design_options` (schema v5); images record their take, draw and
pick. The export puts each item's pick in `05_assets/reference_images/`, writes the
chosen takes to `04_design/selected_designs.md`, and puts every other option in
`05_assets/options/` with `options.csv` (label, profile, edited_from, draw, seed,
prompt). A picked take with a hand-edited prompt has that prompt in `selected_designs.md`.

### Shot keyframes: a checked frame per sub-shot, for video

A storyboard shot is one video generation, but a dynamic one holds a few cuts inside it
(0-2 s a wide, 2-6 s a close-up), which the video prompt marks as `[Shot 2] At 00:02.000,
the camera cuts to ...`. The **Keyframes** tab gives video production (dramer) one checked
picture per cut, so characters stay consistent and each cut starts as planned.

The tab opens on a **Gallery**: the keyframes one after another in story order, the current
one large between the cut before and the cut after (to check that consecutive frames
match), over a filmstrip of every cut with the scene breaks marked. The arrow keys (or
Previous / Next, or a click on the filmstrip) step; **Follow** steps through one person's
cuts only; **Export only** skips what isn't approved; the picture opens full size. The
**Grid** shows every cut's keyframe at once in export order (the
approved draw; else the newest, dashed; else what it waits for), grouped by **scene**, by
**person** (each person's cuts across the story beside the design pictures they were drawn
from, to check that they stay the same person) or by **place**. **S/M/L** sets the tile
size, **Export only** shows just the approved keyframes the export holds, a tile opens full
size (the arrow keys step through its row) and **Details** opens the cut in the detailed
view (plan, takes, prompts and checks). The view settings are kept in the browser.

**Inserting a cut between two keyframes.** Grouped by scene, each Grid tile has a **+** on
its right edge: a new cut after that one, in its shot (after a shot's last cut, at the end of
that shot). Say what the new cut shows; the text model writes it (framing, camera, action,
who is where) and re-times the shot, rewording the actions next to it so the shot is told
once, in order (`POST .../keyframes/{shot_id}/insert` with `after`, the cut it follows, and
`note`). The other cuts keep their view word for word, so their keyframes stay approved,
moved to their new numbers. The cut right after the new one was drawn from the cut before
the insert: it stays approved but out of date ("a cut was inserted before it") until it is
redrawn from the new cut's keyframe, and approving the new cut doesn't withdraw it or start
a redraw of it.

**Approval is a plain switch.** Any finished draw can be approved, one per cut, and
withdrawn; approving or withdrawing one never changes another cut's approval. What a
keyframe was drawn from is a note, not a rule: a cut whose approved keyframe doesn't
continue from the approved keyframe before it (the cut before was re-approved, withdrawn,
deleted, or has no keyframe yet) stays approved and is marked out of date, in the window
and in the export's warnings, until it is redrawn or the editor leaves it so. With **Draw its keyframe** (`draw`, the default) the new cut is drawn once
planned, if the cut before it is approved. A shot holds at most 5 cuts, each at least 1 s
inside the clip; a plan that can't fit keeps the shot as it was and says why. The plan from
before the insert goes to the shot's plan history.

Each keyframe is made in steps:

1. **Plan the cuts**: the text model splits each shot's clip into 1-3 sub-shots (a shot
   holds up to 5: asked for by count, written by hand or inserted), each with
   its start, framing, camera, action, who is on screen and on which side of the frame,
   and who is off frame on which side. **Edit** changes them (and the clip's length); a
   shot with no plan is one sub-shot. Changing a sub-shot withdraws its approved keyframe
   and those after it. A plan should be a clean spec (see below): the planner gets one more
   try with the problems, and a model plan that still isn't clean is kept but flagged on the
   board and in the export (`clean: false`) for you to fix in **Edit**, where a save that
   isn't clean is refused with the reasons.
2. **References**: the design picks of the sub-shot's people, in the order dramer sends
   them: each person in this shot's look, the set's wide view, the props, then faces while
   nine pictures allow. From sub-shot 2 on, `<Picture 1>` is the approved keyframe of the
   sub-shot before, so light, wardrobe state and set dressing carry across the cut.
3. **Write, draw, check**: the text model writes an H3 edit prompt for the sub-shot's
   first frame; 1 to 4 draws at the video's aspect ratio at about 4 MP (1536x2720 for 9:16:
   several times the video's resolution, and light enough that nine pictures fit one
   request), with MiniMax H3 or, chosen per take ("Drawn with" on the board, `provider` in
   the API), Nano Banana (Gemini's image models, at most eight pictures: the last are
   dropped); a vision judge checks
   each draw item by item: each person's face and wardrobe, people count, placement,
   framing, action, set, props, defects, stray text, style. Each failed item costs points
   (major 25, minor 8); a draw passes with no failed major item and a score of 80 or more.
   A check that can't run never counts as a pass (**Check again**).
4. **Revise**: when no draw passes, the failed items and the judge's fix go back to the
   writer for the next round, drawn with the same seeds; up to 3 rounds, then a person
   decides. A direction starts a new written take at any time.
5. **Prompt versions**: every prompt the writer writes and every edit you save is a version
   of that sub-shot's prompt (v1, v2, ...). The **Prompt** panel lists them (who wrote each,
   from which version, your note), compares any two word by word, and saves your edit as the
   next version. **Draw vN** draws a version as written with the last take's seeds, so the
   draws compare one for one; each take says which version it drew. Restore an old version
   by drawing it or saving it again.

Prompts follow the H3 image rules (MiniMax's edit form). The program writes each picture's
definition and relationship sentence, so every picture is used for what it is (a face
picture always transfers that exact face: "<Subject 9> supplies the exact facial features
transferred to <Subject 1>"); the writer adds the target overview, the frozen moment and
the light. A rule check flags what H3 handles badly: a missing definition, relationship,
overview or closing sentence, negations ("no", "without", "nobody": H3 has no negative
prompt and draws what is named), instructions ("keep", "frame", "retain": narrate the
finished image), story or intent words, and a face picture used as a mere style reference.
A written prompt that breaks them is sent back to the writer once with the findings; what
remains is shown on the version and the take. Keyframes use their own content rule, not
the image chats' restrictive mode: the picks are the production's chosen designs, so
faces and clothes are carried over exactly; rules 1 to 3 still apply.
6. **Approve** one draw per sub-shot; the next sub-shot then starts by itself. **Plan cuts
   and draw first keyframes for every shot** does steps 1-4 for every shot at once.

The export's `05_assets/shot_keyframes/` has every approved keyframe of the current plans
(`<shot>_s<N>.jpg`), `index.csv` (sub-shot, start and end, `timecode` for `[Shot N] At ...`,
framing, camera, action, people, references as exported files, seed, check result, score,
failed items, likeness, motion, prompt) and `sub_shots.json` (every shot's plan, with each
sub-shot's keyframe file or null). Even a draw a person approved after it failed the check
is exported, marked failed.

**Which pictures each keyframe is drawn from: reading the scene.** A character has a few
design pictures (a face, a picture per look), and the storyboard names one look per shot; that
is the intended outfit, not always what the camera sees (a work look with pinned hair and a bag
while she is still dressing). So before a scene's first written take, one text call
(`shot_reading`, **gpt-6-luna** unless `MCPHUB_MODEL_SHOT_READING` says otherwise) reads the
whole scene in order, with every candidate picture attached, and decides for each person on
screen in each sub-shot:

- their **state** at that moment: what they wear (shoes or bare feet), their hair, what they
  hold, their condition, carried over from the moment before unless the action changes it;
- the **closest picture** of theirs (clothing matters most, then hair; what is held and the
  condition are easy to change in words), what it already gets right, and why.

The prompt's sentence for that picture keeps the face, build and what it gets right and states
the rest ("... wears an ivory blouse and charcoal trousers, barefoot, hair loose, with empty
hands"), so a picture's bag or pinned hair isn't carried into a moment that doesn't have them;
the writer and the judge both see each person's state, and the judge checks the wardrobe
against it. Each sub-shot on the board shows the choice, the state and the reason, with a menu
to draw a person from another of their pictures (**Read the scene again** keeps your choices).
A reading is redone when a shot of the scene is re-planned or a pick changes; if it can't be
used, the storyboard's looks are. A shot's first cut also starts from the approved keyframe of
the shot before it in the scene, when there is one, so light, set dressing and everyone's state
carry across the cut. Each prompt version keeps the pictures it was written for. The export's
`sub_shots.json` gives each cut's `now`: every person's state and their chosen picture.

**Keyframes are made in two phases, and the editor locks the shot between them.** In the
first, the script's plan and prompts draft rough keyframes; the editor reviews the sequence
in the Gallery (**Looks right** approves a cut, **Fix** opens it to chat, redraw or pick
another draw, **+** inserts a cut). The judge's check is advice while drafting: an approved
keyframe is the shot, whatever the check said of it against the plan (it shows as "differs
from the plan", not as a failure). Once every cut of a shot is approved (phase chip
**Review**), **Read keyframes** has the vision model read each approved keyframe and write
down what it shows in the plan's own words (shot size, camera position and height, who is
where, clothes, props, background), with warnings for what changes between the cuts (a
person who swaps sides, a prop that vanishes). The reading is shown next to the plan, cut by
cut, **As planned → As drawn**, and every field is editable. **Lock shot** makes the
corrected reading the plan's view (`source: keyframes`; timing, action and lines stay): the
draft plan goes to the plan history, the keyframes stay approved, and the video prompt is
written again. A locked shot can't be re-planned, edited or added to; to change a cut, fix
or redraw its keyframe (which makes the reading stale and the video prompt out of date);
**Unlock** restores the draft plan. The export marks each locked shot (`locked`, `planned_by:
keyframes`) and counts them in `readiness.shots_locked`.

**Each shot's H3 video prompt is written here, from its keyframes.** Once every cut of a
shot has an approved keyframe, the text model writes the shot's MiniMax H3 video prompt (the
`video_prompt` step, `shots/video_prompt.py`). The plan drafts the keyframes; once a cut has
one, the keyframe drives its prompt: the writer is shown each cut's keyframe (and what its
check found wrong against the plan) and describes the cut as the picture shows it (shot size,
camera position, who is where, clothes, what is behind them), while the plan gives the action,
timing, lines and camera movement. A keyframe the story can't use as it is (the wrong person,
a prop missing) is reported in the prompt's notes for a redraw. The prompt follows the H3
video prompt skill dramer's renders were measured against: where the camera stands before the
set, every person bound to their pictures and clothes in one sentence, every line quoted word
for word after "says in English:" with whose lips move, only what is there (never what isn't),
the action timed to the cut, one camera sentence in MiniMax's wording, and one sound sentence
last. It is one block per cut, with no timestamps (dramer adds "[Shot N] At <timecode>" from
the plan), and names pictures as `{pic:...}`. A program then checks it: each cut's lines word
for word and in order, every picture sent named and no other, no one who isn't in the shot
named (not even "off frame"), a camera sentence per cut, and under H3's 7,000 characters; and,
as advice, negations, a speaking verb with no quoted line, a silent cut that doesn't say so,
missing lips or sound sentences, camera wording, and 150 to 500 words. With findings, it is
written once more with them, and the better of the two is kept. On the Keyframes tab
(**Details**), each shot shows its prompt with the pictures as labels, its state (not
written, writing, current, out of date, failed), its findings and the shot's dialogue pace
(more spoken words than the clip holds at about 2.5 words a second). **Write prompt** / **Write
again** writes it sooner or again, and **Write video prompts** writes every shot's that is
missing, failed or out of date. A prompt is out of date as soon as what it was written from
changes (the plan, an approved keyframe, a chosen picture, a line).

**The shot specs are the contract for video production.**
`03_storyboard/shot_specs.json` (`spec_version` 3) gives, per shot, everything dramer makes
the clip from, so dramer stores and sends it instead of working it out again:

- `prompt` (new in version 3): the shot's video prompt in format `h3-paragraph/1`: `blocks`
  (one per cut: `sub_shot`, `text`, `camera`, the camera sentence kept apart so a series
  style can swap it), `sound`, `notes`, `advice` (the check's weaker findings), `model` and
  `written_at`. Pictures are `{pic:<file name>}`, the name of a file in `references`;
  dramer turns each into `<Picture N>` in the order it sends and drops the sentences of a
  picture it doesn't send. Null when the shot has no current prompt that passes the hard
  checks (`not_ready` says why); dramer then writes one itself;

- `spec`, in dramer's shape: `summary`, `action`, `camera`, `lighting` (from the set sheet),
  `target_duration_ms` (the plan's clip, or the storyboard's length for an unplanned shot),
  `dialogue` (each line once: a line that runs over a cut is spoken in the shot it starts in,
  and the next shot's `continuity_notes` says it carries over), `sound_notes`,
  `continuity_notes`, and `sub_shots` (the cuts below, each `keyframe` a path, each `now` the
  people's states; null for a shot nobody planned that has no keyframe);
- `references`: the pictures to send, in priority order, each with `file` (a path from the
  export's root), `kind` (keyframe, chosen, look, face, set, prop), `entity_id`, `name`,
  `sub_shots` (the cuts a keyframe or a chosen picture is for), `likeness` and `named`
  (whether the prompt names it; null without a prompt). Keyframes and faces always come first
  (`shots/clip_refs.py`), so a consumer that sends fewer keeps those. A shot whose every cut
  has an approved keyframe is `keyframe_first`: its keyframes, the faces, then the props,
  each person's picture and the set. Any other shot: its keyframes, the faces, each person's
  picture (the reading's choice, else the storyboard look), the set, the props, then the
  pictures chosen for later cuts. Keyframes are references the
  video matches, not start frames. At most `max_references` (9, what dramer sends H3 with a
  clip) are listed; those that didn't fit are in `references_left_out`, and a consumer with a smaller
  limit drops from the end;
- `cast`: each person, the place and each prop with a note (a person as the reading found
  them in their first cut, else their storyboard look);
- `ready`, and `not_ready` saying why not: the plan's problems, each cut with no approved
  keyframe, any line the shot list cues that the script no longer has, keyframe work still
  running on it (`keyframe_work_running`, a prompt being written included), and no current
  video prompt;
- `warnings`: worth a look, not blocking: more spoken words than the clip holds at a slow
  read (about 2.5 words a second);
- `spec.carried`: the lines spoken in the shot before that run over the cut into this one
  (also noted in `continuity_notes`).

At the top, `pace` is the story's pace and `readiness` says how far the keyframe pass has got:
shots and shots ready, shots with a prompt, cuts planned and cuts with an approved keyframe, shots with keyframe
work still running (a plan being written, a keyframe drawn or checked), and a `note` for
production: still running (don't render from this revision yet), complete, or what is missing.
People in each cut's `people`, `placement` and `off_screen` are named as on the cast sheet,
whatever name the planner used.

**The sub-shot spec** (`05_assets/shot_keyframes/sub_shots.json`, `spec_version` 1) is the
earlier form, kept for older importers. Draper runs first: its plan
says how each shot is cut, and video production (dramer) writes its prompt from it rather
than re-timing the shot. `05_assets/shot_keyframes/sub_shots.json` (`spec_version` 1) gives,
per shot, `clip_ms` (render exactly this long: H3 takes whole seconds from 4 to 15; the
default is the storyboard's duration rounded up), `clean` and `problems`, and per sub-shot
`index`, `start_ms`/`end_ms`, `timecode`, `framing`, `camera`, `action`, `people`,
`placement` (name: left, center or right), `off_screen` (name: left or right), `lines`
(speaker, line, start and end in the clip, and `where`: on screen, off screen left/right,
or voice) and `keyframe` (the approved first frame, or null). The video prompt then has one
`[Shot N]` per sub-shot at its timecode, with that sub-shot's framing, positions, action and
lines, and cites its keyframe as the reference frame of `[Shot N]` (a picture the cut
matches, not a start frame). A clean spec:

- the clip is whole seconds from 4 to 15, and every line lies wholly inside it (a
  storyboard shot longer than 15 s is flagged: split it in the storyboard);
- no cut falls in the middle of a line;
- each line's speaker is on screen, or placed off frame, or a voice from outside the shot;
- a line spoken by one of the shot's people is spoken with them in frame, or with nobody in
  frame (an insert): H3 lip-syncs a quoted line to the face it sees, so a listener never holds
  the frame through someone else's line; a reaction goes between lines;
- every on-screen person has a side of the frame, and only they do;
- every cut changes the view (no two sub-shots in a row share a framing);
- screen sides hold across cuts (the 180-degree rule): nobody jumps from frame left to
  frame right, on screen or off.

The planner covers dialogue speaker by speaker (a cut to the speaker shortly before their
line) unless the storyboard asks to hold a two-shot. It plans at the story's **pace** (below):
the long setup goes to whoever acts or speaks, a listener has something to do, most sub-shots
move the camera, and a reaction is held no longer than the pace allows. A plan that isn't a
clean spec (a listener held through someone else's line, a cut in the middle of a line) goes
back to the planner once with the problems (the better of the two plans is kept); what is
still wrong is flagged on the board, and an episode run holds the shot there for a person.

**Plan history.** A shot's cut plan is never lost when it is replaced: a re-plan, an edit
with different cuts or a restore keeps the plan it replaces in the shot's history, with the
keyframes approved for it then (`shots/history.py`). **History (N)** on the shot lists the
earlier plans, newest first, each with its cuts; **Restore** makes one the plan again. Its
keyframes come back (takes are never deleted when a plan changes, only hidden), approved
again where the draw still exists, and the plan in force takes its place in the history (so
swapping back and forth never piles up copies). A plan whose cuts no longer fit the shot (a
revision changed who is in it or how long it runs) is refused. A failed plan or a save of
the same cuts adds nothing. The history moves with the story when a
revision is published. Endpoints: `GET .../keyframes/{shot_id}/plans` and
`POST .../keyframes/{shot_id}/plans/{version}/restore`.

### Generate episode: clips and the assembled episode

The **Episode** tab has one button, **Generate episode**, that takes an episode from its
storyboard to its clips and a rough assembly (`shots/episode.py`). The run is a loop over
the episode's shots that reads the keyframe board as it is and does the next thing each
shot needs, with the rules a person clicking would use (`shots/flow.py`): the design
pictures are drawn if any are missing; each shot's cuts are planned; each cut's keyframe
is drawn and the best draw approved; the video prompt is written once every cut is
approved; the clip is rendered once the prompt is current and passes its checks; and when
every shot has its clip, the clips are cut together in order (`shots/assemble.py`, ffmpeg:
each clip scaled to the first one's size, with its own sound; no music or titles). Nothing
is cached between steps, so whatever you do meanwhile on the Keyframes tab (a new plan, a
different approval, a redraw, an edited prompt) is what the next step sees, and a run
started again keeps every approved keyframe, current prompt and current clip.

The page shows the plan and the progress: per shot its stage (planning, keyframes, prompt,
rendering, done, waiting), the approved keyframes, the prompt's state, the clip with its
first frame, the counts, the time so far and an estimate from what this episode's work
took. **Pause** stops the loop (what is already running finishes); **Stop now** also pauses
the running takes and drops the clip being rendered; **Resume** goes on from the board as
it is. **Skip** leaves a shot out of the run and the assembly; **Retry** forgets a shot's
failed tries.

The run **holds** where a person is needed, and says why on the shot's row: a cut whose
takes failed twice, a prompt that fails a check, a design pick that is missing. It also
holds where the settings say: *Approve keyframes myself* (every cut is drawn, then waits
for your approval on the Keyframes tab; **Approve for me** lets the run approve that shot's
best draws, **Approve the rest for me** every shot's), *Hold before rendering* (video
renders are the slow, paid part) and *Hold before assembling*. A hold is released from the
page and the run goes on. Without holds, the best-scoring draw of each cut is approved,
including one that failed the check (*Approve a failed check's best draw*, which can be
turned off).

**Clips.** A shot's clip is rendered from its stored video prompt: the blocks become one
paragraph (`[Shot 1] ...`, `[Shot 2] At 00:02.000, ...` from the plan's timecodes), each
picture becomes `<Picture N>` in the order the pictures are sent (`shots/clip_refs.py`:
keyframes first, then faces), the sentences of a picture that isn't sent are dropped, and
the sound sentence comes last (`shots/clips.py`). The pictures go with the prompt as
**reference** pictures (MiniMax H3 reference-to-video), never as start frames. The clip is
whole seconds, 4 to 15, at the story's aspect ratio. A clip is *current* while the prompt
it was rendered from is the shot's current prompt and the same pictures would be sent; a
change to either marks it out of date, like a video prompt. The Keyframes board shows each
shot's clip (`clip`), and **Render again** renders one shot now.

**Takes.** A run renders `takes` takes of every shot's clip (a run setting, 1 to 4, one
after another, each with its own seed) before it assembles; the counts show takes done
against takes asked. A shot can have several clips: each render is a *take* with its own seed
(`POST /v1/drama-packages/{id}/clips/{shot_id}` with `takes` 1 to 4, `fast`, and an
optional `seed`; without one, seeds the shot hasn't used, so rendering again gives a new
clip rather than the identical one). Clips render one at a time; at most four of a shot
queue at once. The Episode tab's Clip step shows every take side by side: a gray box with
its clock while one renders (and how many are ahead of it), then the video, with the newest
finished take marked *In the episode*. **Use** makes an older take the newest, so the
episode is cut from it (`POST /v1/shot-clips/{id}/use`). The Prompt step's **Edit** opens
the video prompt in place: a field per cut, its camera sentence and the sound, with the
pictures it may name; saving (`PUT .../keyframes/{shot_id}/video-prompt`) runs the same
checks as a written prompt, marks it current to the shot, and puts its takes out of date
until **Save & render** or **Render again**. Each take records its render statistics
(`ShotClip.started_at`, `finished_at`, `bytes`, `width`, `height`, `media_s`, with
`render_s` and `queue_s` in the view): shown under the take as when it was queued, began
and ended, how long the render took and the wait before it, the picture size, the file's
length and size.

**Steps.** The sampling steps asked of the video model are a run setting (`steps`, 1 to
100, empty for the model's own default) and a field of a render by hand; each take records
the count it was rendered with (`ShotClip.steps`, shown in its caption). Fewer steps render
faster: the measurement in `docs/render-speed.md` found 14 steps indistinguishable from the
route's default of about 50 at 3.5x the speed, so `DRAMA_VIDEO_STEPS` defaults to 14 for
renders that don't say (`default` leaves it to the route).

**Trimming and music.** When the episode is cut together, each clip is trimmed to its
sound (`trim`, a run setting on by default): a rendered clip warms up from a posed first
frame and holds its end state after the last word, and that dead air made every cut breathe
out; the cut now runs from 0.25 s before the clip's first sound to 0.6 s after its last,
never below half the clip (a silent action clip keeps its length). `assemble.trim_points`
reads the sound with ffmpeg's silence detection. The run's output says how much was
trimmed. With a `music` caption (a run setting, e.g. "slow warm piano and strings,
hopeful"), a music bed is composed per scene with ACE-Step through MCPHub's gpu-music
route (`musicgen.py`; `DRAMA_MUSIC_CLIENT=mcphub`, else an offline fixture tone), the
scene's length plus two seconds, one seed per scene so the scenes differ, and laid under
the scene from its first clip, faded in and out, mixed at `music_db` (default -16 dB)
under the dialogue. Composed beds are kept on the run (`EpisodeRun.music`) and reused while
the caption and the scene's length hold.

**Coverage.** The storyboard is told how a scene is covered for a vertical frame
(`pace.COVERAGE_RULES`): shots of 3 to 6 s with one or two beats, a wider shot to place the
people and then singles, over-the-shoulder shots and a close-up that alternate with the
lines, medium close-up as the default size, each `framing` beginning with its size and
subject. A hard check (`check_coverage`) refuses two consecutive shots of a scene with the
same size and the same people, and a scene with more than half its shots as two-shots or
group shots; the review reports shots over 8 s without a camera move that carries them.

**Speech check.** H3 lip-syncs a quoted line to the face it sees, so a take can put a
line on the wrong person, or have someone talk in a cut meant to be silent. After each
render (`check_speech`, a run setting on by default, and `check` on a render by hand) the
runner samples frames over every line (three per line, two per silent cut, twelve at most,
`shots/speech_check.py`) and shows them to the vision model with the faces sent with the
clip; it says whose mouth moves in each cut. The findings are rules on that answer: an
on-screen line's speaker is seen speaking (minor when not: a sampled frame can fall between
words) and nobody else is (major); a cut with no line spoken in frame has nobody talking
(major). A take that fails is *rejected* (`ShotClip.check`, state `rejected`): it isn't
counted as a take, the episode isn't cut from it while the shot has another, and the run
renders another take with a new seed, then holds for a person as it does for a failed
render. The Episode tab marks the take *wrong speaker* with the findings; **Use anyway**
accepts it (`ShotClip.accepted`). A check that couldn't run leaves the take standing,
marked *unchecked* with the reason. The Cuts step lists the shot's sub-shots and
embeds the board's cut plan editor (**Edit**), with **Plan cuts** and the pace and cut
count for the text model; a plan locked to its approved keyframes (as every shot's is
after a run) needs **Unlock** first, which restores the draft plan and so puts the prompt
and clips out of date. A changed sub-shot loses its approved keyframe, like on the board.

The **Fast** option (a run setting, and `fast` on a single render) renders sooner and
rougher: the short edge drops to `512` (from the client's default) and only the identity
anchors are sent, every cut's keyframe and then the face of each person on screen, within
the usual cap of nine, each picture shrunk to a `768` short edge (JPEG, by ffmpeg) since
the model works at its own size and more pixels are only upload. Resolution and clip
length are the real levers on render time, the reference count a small one; and a cut
without its keyframe, or a person without a face picture, drifts to another face (an
early three-picture cap gave a close-up its scene partner's face). A sentence of the
prompt that cites both a sent and an unsent picture keeps the sent one; only a sentence
about unsent pictures alone is dropped. A clip's print records whether it was rendered
fast, so switching the option marks the existing clips out of date.

The video model is chosen like the text and image models, opt-in: `DRAMA_VIDEO_CLIENT`
is `fixture` (default: plain coloured clips of the right length, made with ffmpeg, so the
flow runs offline and in tests), `mcphub` (MiniMax-H3 Ref2VA on MCPHub's GPU route, the
company's own H100s: `MCPHUB_KEY`; `MCPHUB_VIDEO_URL`, default `https://api.mcphub.com`;
`MCPHUB_VIDEO_MODEL`, default `minimax-h3-ref2va`; `DRAMA_VIDEO_SHORT_EDGE`, default `768`;
`DRAMA_VIDEO_SEED`, default `1`, the same request and seed give the identical clip) or
`minimax` (MiniMax's video API: `MINIMAX_API_KEY`; `MINIMAX_VIDEO_URL` for a gateway,
default `https://api.minimax.io`; `MINIMAX_VIDEO_MODEL`, default `MiniMax-H3`;
`DRAMA_VIDEO_RESOLUTION`, default `768P`). `DRAMA_VIDEO_POLL_S` sets the polling interval.
Renders go one at a time; the MCPHub server is shared and renders one clip at a time for
everyone, about 3.5 minutes for 4 s at 768p, so a job may wait behind others. The prompt sent is the paragraph form dramer renders; how it
measures against MiniMax's own six-field reference format is not evaluated yet.

API: `GET /v1/drama-packages/{id}/episodes` (each episode with its newest run);
`POST .../episodes/{episode_id}/run` (settings: `draws`, `max_rounds`, `provider`, `holds`
of `approve` / `render` / `assemble`, `hold_shots`, `skip_shots`, `approve_failed`,
`render`, `assemble`, `parallel`) and `GET .../run` (the newest run with each shot's stage,
counts and estimate); `GET /v1/episode-runs/{run_id}`, `POST .../pause?now=`, `.../resume`,
`.../release {key}` (`<shot>:approve`, `approve`, `<shot>:cut<i>`, `<shot>:prompt`,
`<shot>:clip`, `render`, `assemble`), `.../retry {shot_id}`, `PATCH .../settings`,
`GET .../output` (mp4) and `.../poster`; `GET /v1/drama-packages/{id}/clips`,
`POST .../clips/{shot_id}` (render one now), `GET /v1/shot-clips/{id}/content|poster`,
`DELETE /v1/shot-clips/{id}`. Runs and clips are kept per revision (`episode_runs`,
`shot_clips`) and move forward with a published revision; a server restart pauses a running
run (resume it) and fails the clip in flight.

### Pace

A story's pace (job option `pace`, under Options; changeable on the story's Overview, like the
rating; `GET/PUT /v1/drama-packages/{id}/pace`) sets how the storyboard and the cut plans keep
the clips moving (`pace.py`). Every pace holds the same rules; the pace sets the numbers:

| Pace | Longest reaction | Shots that move the camera | Action per shot |
| --- | --- | --- | --- |
| `brisk` (default) | 1.5 s | at least three in four | 45 to 70 words |
| `measured` | 2.5 s | at least half | 40 to 60 words |
| `slow_burn` | 4 s | at least one in three | 35 to 55 words |

The storyboard step writes each shot's action as physical business (hands, props, movement,
not a state of mind), gives the screen to whoever acts or speaks, moves the camera (a locked-off
camera says why), and reads each shot against its neighbours for continuity: an object used or
dropped was picked up, a framing names only who is on screen from the first frame, and the
background behind each person holds while they keep their places. The creative review reports
breaks in continuity (`shot_continuity`) and pacing (`pacing`) shot by shot. A changed pace
applies to the next storyboard revision and the next cut plan; shots already planned are not
changed.

**Per shot.** Next to **Plan cuts**, a shot can have its own **Pace** (else the story's) and
**Cuts**: exactly 1 to 5 sub-shots, or auto (the planner decides, at most 3). The planner is told
both; a plan with another number of cuts goes back to it once, and the closer plan is kept.
They are saved with the shot's plan, so a later re-plan uses them again until changed, and
the shot's own pace is what the planner is told. `POST .../keyframes/{shot_id}/plan` takes
`pace` and `cuts` (null: the story's pace, the planner's count; left out: as the plan has
them); `shot_specs.json` gives each shot's `pace`.

Keyframes are production assets: the restrictive content rules always apply, and a shot in
a scene staged as implied or simulated intimacy is never drawn from a pick that shows a real
person's likeness (content rule 3). Endpoints: `GET/POST /v1/drama-packages/{id}/keyframes`
(board; plan and draw every shot without a first keyframe), `POST .../keyframes/{shot_id}/plan`
(model plan, or `sub_shots` of your own), `POST .../keyframes/{shot_id}/prompts` (save a
prompt version), `POST .../keyframes/{shot_id}` (a take on a `sub_shot`: written, or a
`prompt_version` drawn as is), `POST|DELETE /v1/shot-takes/{take_id}/approve`, `.../check`, `.../retry`, `.../pause` (a take being made, its automatic rounds included, stops after the draw or prompt in progress and is `paused`: its finished draws are kept and can be approved) and `.../resume` (its remaining draws, its check, then its rounds, same seeds),
`DELETE /v1/shot-takes/{take_id}`, `GET /v1/shot-takes/{take_id}/draws/{n}/{content|thumb}`,
`POST .../keyframes/scenes/{scene_id}/reading` (read the scene again) and
`POST .../keyframes/{shot_id}/choice` (draw a person from another of their pictures).

### Production hand-off zip

**Download zip** on a job page (or `GET /v1/drama-packages/{id}/export?job_id=…`) gives the
team one file to produce and shoot from. Inside a folder named after the title and revision:

| Folder | Contents |
| --- | --- |
| `00_README.md` | Title, logline, counts, hard-check status, and a guide to the folders |
| `01_story/` | Story bible: synopsis, characters (goals, relationships, arcs), locations, props, episode outline |
| `02_script/` | One screenplay-style script per episode (scene headings, action, CHARACTER cues, V.O.) + JSON |
| `03_storyboard/` | `shot_list.csv` (one row per shot: timing, framing, camera, who and what is on screen, dialogue with times, sound, continuity, asset IDs), `storyboard.md`, `dialogue_schedule.csv`, JSON, and `shot_specs.json` (per shot, its complete spec for video production with its references in order) |
| `04_design/` | Cast, set and prop sheets (Markdown + JSON) |
| `05_assets/` | Asset requirements (CSV + JSON) and the newest finished reference image per deliverable, with `index.csv` (prompt, seed, size, model, `likeness`, and `source`: generated or imported); `shot_keyframes/`: the approved keyframe of each sub-shot (cut) of each shot, with `index.csv` (timecode, framing, action, references, seed, check result, score, failed items, likeness, motion, prompt) and `sub_shots.json` (every shot's cut plan) |
| `06_qa/` | Hard checks and creative review |
| `07_sources/` | The idea text and uploaded photos/videos with their prompts |
| `08_intimacy/` | Only when the story has intimacy: `intimacy_plan.md`, every such scene with its level, what is shown and implied, nudity, closed set and consent beats, for the director and intimacy coordinator |
| `package.json` | The whole package |

Everything uses names, not IDs, where people read it. CSVs are UTF-8 with a BOM so Excel
and Google Sheets open them correctly. Archived stories can't be exported.

### Intimacy and content rules

Intimate scenes are written as staging for the crew; the app never draws them.

- **Content rating** (job option `content_rating`, under Options; changeable on the story's
  Overview): the MPA film ratings. `G` and `PG` allow at most a kiss; `PG-13` (default) at
  most *implied* intimacy (the scene cuts away), no nudity; `R` allows simulated intimacy
  with at most partial nudity, on a closed set. Images follow the rating too: suggestive at
  PG-13 and up, nudity only at R, never explicit, and never anything sexualized of a real
  person's likeness (docs/content-safety-and-likeness.md).
- **Scenes** carry an `intimacy` block when they have physical intimacy: level (`kiss`,
  `implied`, `simulated`), who, what the camera shows, what is implied, nudity, consent
  beats and closed set. The script tab marks these scenes.
- **Hard checks:** a scene above the job's rating, nudity the rating doesn't allow, an
  implied or simulated scene without consent beats, simulated intimacy or nudity without a
  closed set, or an empty `on_screen` fails the script step, which is retried with that
  feedback. Everyone in an implied or simulated scene needs an apparent age in years on
  the cast sheet; anyone who reads as under 18 blocks the job (`intimacy_minor`), and the
  fix is to remove the intimacy from the script, never to restate the age.
- **Export:** `08_intimacy/intimacy_plan.md` (text only) lists each scene for the
  intimacy coordinator, with the on-set rules.
- **Images** (`content_guard.py`), in every mode: never nudity or sexual acts; never
  romantic, sexualized or revealing images of anyone under 18 or who looks it; never a
  **sexualized** image of a real person's likeness (lingerie or underwear, undressing, bed
  scenes, suggestive poses, degrading situations). A real person's likeness is an
  identifiable person from an uploaded photo (a visible face counts), or someone made to
  resemble a named real person; an upload used only for its clothing, a place or an object
  passes on nobody's likeness. In Experiment, each result records whether it shows a real
  person's likeness (marked "real-person likeness"); images built on it keep that, until you
  go back to an image without one.
  A word-list check runs on every prompt before any render (design board and Experiment);
  "nude" as a color ("nude lipstick") and negations ("no nudity") are not refused. The design
  board always uses the restrictive rules (its images go into the hand-off package): for a
  job with uploaded photos or videos used as character references (or with no stated use),
  prompts that mention underwear, lingerie, swimwear or bed scenes are refused, and the
  prompt writer never makes a character resemble a named real person. Location and style
  references don't count as people. The text model judges the rest: in
  Experiment it sees the pictures; for Rewrite with GPT the writer applies the rules, while
  exact prompts and hand-edited takes are screened separately. The check fails closed, and
  a refused message is marked "not allowed"; **Check again** runs it under the current rules.
- **Restrictive mode (Experiment)** (switch at the top of the page, off by default,
  remembered per browser; the design board always uses the restrictive rules):
  - **Off (exploration):** a character may resemble a named real person or take a face from
    an uploaded photo, including in clothed romantic scenes (a kiss, an embrace, swimwear).
    Clearing likeness is production's and legal's call before anything is used.
  - **On:** GPT turns "like <a real person>" into generic traits and keeps real names out
    of prompts; an existing real-person likeness gets no romance or swimwear either.

### Characters and Sets: layers

A character or set page opens in layers (`docs/library-layers.md`): the character with its
looks and items as cards, then one look (its line, sheet and shots), then one of its
pictures with its takes. **+ New look** (or **+ New state**) adds one by hand; it's drawn
from the face like any look. On the Keyframes tab, **Send to a look…** adds a keyframe
draw as a take of a look its shot wears (or of its set), as the pick only when asked.
A look's page also lists the shots it is in with their keyframes; each opens a page to
review and develop them: approve, draw rounds, or **Develop in a chat** and bring a chat
image back with **Use as keyframe**. **+ New item** adds a prop a character carries
(and can start its picture right away, by chat, photo or text). Every picture (face, look, set view, prop) has the same
workbench: **Develop in a chat**, **From a photo** and **Create from text** (a study, as Face
Lab, described and tested as that kind of picture; a look is tested on the face's pick), with
its history of chats and studies underneath. The story's Casting tab lists its Props as cards linked to
their item pages, and each item page links back.

### Experiment: chat with the image model

An image chat is a conversation with MiniMax-H3-Image, one change per message. It lives on
the **Characters** and **Sets** pages (the workbench, `docs/workbench.md`):

- **Develop in a chat**, above each picture, starts a chat from that picture's pick.
  **Use as pick** on a result makes it the picture's pick, and what was drawn from the old
  one is marked out of date.
- **Scratch**, in the sidebar, holds chats tied to no picture (**New chat**). Link one to a
  picture with **Link a scratch chat…** on that picture's row.

Chats open in a side panel over the page. **Open full page** shows a chat on its own
(`#exp=<id>`), with every chat listed on the left. The steps below are the same in both.

1. **New chat**, then attach a base image (button or drag and drop) and send it without
   text: it becomes the *current image* (outlined, marked "current").
2. Describe a change ("gather her hair into a low bun at the nape"). The current image is
   sent as `<Picture 1>`; images attached to the same message follow as `<Picture 2>`, ...
   With no current image, a message makes a new image (text to image).
3. **Rewrite with GPT** (default): the text model looks at the pictures, the prompt that
   made the current image and the earlier messages, and writes a full H3 edit prompt
   (what is preserved, what changes) with the shared H3 rules; its note says what it
   changed. **Exact prompt** sends your text as written. Every reply shows the prompt
   sent; **Edit this prompt** copies it into the box in Exact mode.
4. Every image in a chat (results and uploads) gets a number, shown as a badge (`#7`);
   numbers are never reused. Refer to earlier images by number: "hair like #3" sends #3
   along with the current image, and "go back to #3 and add a hat" lets GPT start from #3
   instead (it chooses which pictures to send and in what order; the reply shows "sent #3").
   Click a badge to put its number in your message. In Exact prompt mode, `#3` is replaced
   with the `<Picture N>` it is sent as.
5. The result becomes the current image, so the next message builds on it. **Edit this
   one** makes any earlier result or upload current instead (branching); **Start a new
   image** clears it. **Draws** (1–4) renders several seeds of the same prompt. In Rewrite mode a message
   can set it too ("do 4 draws", "give me three versions"): the writer returns the count and
   writes one prompt, never a grid of options in one image. **Drawn with** picks the
   image model for the next message, in every chat (Characters, Sets, keyframes, Scratch):
   MiniMax H3 (default) or Nano Banana (when `GEMINI_API_KEY` is set; at most eight
   pictures per message). The prompt is written the same way; each reply says which model
   drew it.

Size: "match the current image" picks the H3 size nearest the current image's aspect
ratio (about 16 MP), or choose one. One message runs at a time per chat; a failed one
can be retried (a prompt already written is kept). With the offline fixture clients the
replies and images are placeholders; real images need `DRAMA_MODEL_CLIENT=mcphub` (or
`DRAMA_IMAGE_CLIENT=minimax`), and each draw is a paid GPU call.

API: `GET/POST /v1/experiments`, `GET/PATCH/DELETE /v1/experiments/{id}`,
`POST /v1/experiments/{id}/turns` (`text`, `attachments` asset ids from
`POST /v1/source-assets`, `mode` rewrite|exact, `draws`, `size`),
`POST /v1/experiment-turns/{id}/retry`, `GET /v1/experiment-turns/{id}/results/{n}/content|thumb`.

### Face Lab: from a photo to a reusable prompt

A face study (Face Lab) turns a photo of a face into the most descriptive text prompt GPT can
write, then tests that prompt by itself, so you can see how close words alone get, and keep
a description that works as a reusable, castable character prompt. Start one with **From a
photo** on a character's face (each render then has **Use as pick**), or with **New study
from a photo** in Scratch. A study opens in the side panel; **Open full page** shows it on
its own (`#face=<id>`).

1. **New study from a photo:** GPT looks at the photo (at up to 1,536 px) and writes a
   text-to-image prompt recreating it: framing and pose, then the face feature by feature
   (skin, face shape, brows, eyes, nose, lips, hair, expression), wardrobe, background and
   light. It never names the person, and its notes say what will be hard to match from
   words. Nothing is drawn.
2. **Describe again**, optionally with a focus ("more detail on the eyes"): GPT improves on
   the last description. **Use another photo** starts over with a new one.
3. **Test the prompt:** edit it, then **Render**. The prompt goes to H3 alone (no
   picture). Choose **Random** seeds with 1–4 draws, or **Fixed** seeds (e.g. `11, 22, 33`);
   **Same as last** reuses the previous test's seeds, so only your wording changes between
   tests. Each test shows the photo next to its renders, with each render's seed and number.
   **Load this prompt** brings back any earlier version.

If the photo shows an identifiable person, renders of its description carry that person's
likeness (content rule 3: nothing sexualized). With **Restrictive mode** on, GPT describes a
type rather than a replica (no distinctive identifying details).

A study is an Experiment session of kind `face`: `POST /v1/experiments {"kind": "face"}`,
`POST /v1/experiments/{id}/describe` (`asset_id`, `hint`, `restrictive`),
`POST /v1/experiments/{id}/tests` (`prompt`, `draws` or `seeds`, `size`, `restrictive`);
`GET /v1/experiments?kind=face` lists studies.

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
`MCPHUB_MODEL_SCRIPT`, `MCPHUB_MODEL_STORYBOARD`, `MCPHUB_MODEL_REVIEW` and the keyframe steps
(`MCPHUB_MODEL_SHOT_PLAN`, `_SHOT_FRAMES`, `_SHOT_CHECK`, `_SHOT_READING`,
`_VIDEO_PROMPT`, `_KEYFRAME_READING`; reading a scene
defaults to `gpt-6-luna`), e.g. a cheaper
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
  -d '{"sources":[{"type":"text","text":"A young woman about to lose her bookstore discovers that the old man who comes to collect the rent is the father who disappeared years ago.","use_as":"story_seed"}]}'
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
    revise.py        The revise run: scoped re-runs, scope and locks put back, publish,
                     the story's work carried forward
  validation/      Deterministic hard checks (spec §8) and ID index
  llm/             Vendor-neutral ModelClient protocol, MCPHub client, offline fixture
  config.py        .env loading and model client selection
  prompts/         Base orchestrator prompt (verbatim from spec §10)
  store/           Repository protocol; SQLite (default) and in-memory backends
  jobqueue.py      Durable job queue (leases, retries, checkpoints)
  media.py         Uploaded images/videos: storage, type sniffing, keyframes, audio
  imagegen.py      Image generation clients (MiniMax-H3-Image on MCPHub, offline fixture)
  design/          Design pictures (characters, sets, props): prompts.py (planning a
                   picture: prompt, references, brief), slots.py (anchors, parents, picks),
                   staleness.py (out of date, redraw in order), takes.py (explore, develop,
                   takes, picks, imports), worker.py, setup.py (thumbnails, image clients).
                   images.py re-exports it all under the old names
  shots/           Shot keyframes: models.py, common.py, picks.py, plans.py, history.py,
                   approvals.py, staleness.py, references.py, takes.py, writer.py, planner.py,
                   judge.py, inserts.py, clip_refs.py (the pictures a clip is sent with),
                   video_prompt.py (the shot's H3 video prompt), lock.py (locking a shot to its
                   keyframes),
                   runner.py (ShotFrameRunner), board.py. shot_frames.py re-exports it all
  library/         The asset library: characters and sets across stories (models, sync,
                   views, matching, reuse)
  shot_reading.py  Reading a scene: each person's state per cut and the closest picture
  shot_specs.py    The export's shot_specs.json for video production
  pace.py          A story's pace: the storyboard's and the planner's pacing rules
  sheet_edit.py    Hand edits to cast, set and prop entries
  experiment.py    Experiment tab: image chat sessions, the GPT prompt rewriter, the runner
  face_lab.py      Face Lab: photo-to-prompt descriptions and seeded prompt tests
  content_guard.py What image prompts may ask for (word lists + a model screen)
  worker.py        Job workers; `drama-worker` entry point
  api/             FastAPI app (app.py builds the shared pieces and registers each group):
                   schemas.py (request/response models), designs.py, experiments.py,
                   jobs.py, packages.py, shot_frames.py (keyframes), library.py
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
- **Revisions are immutable**, except for hand edits to sheet entries, which change a
  revision in place. Packages are stored per revision; a stale `base_revision_id` returns
  409.
- `RevisionRequest.visual_change` is an addition to the spec. It says whether an edit to a
  character/location/prop changes how it looks (which invalidates art requirements) or
  only changes story facts.

## Not implemented yet

- Script files as uploads (`type: script` with an `asset_id`); paste scripts as text.
- Non-speech audio analysis (music, sound effects) for videos.
- Upstream retry: a failing step only retries itself. It never re-runs earlier steps.
