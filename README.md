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
- **Editing by hand.** Each entry on the Cast, Sets and Props tabs has an **Edit**
  button, and every item on the design board has **Edit text** links that open the
  entries it is drawn from (the character, location or prop, and its look or state). You
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
  Assets tab marks each one ("Out of date: …", `outdated` on `GET …/images`) and **Redraw
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

Each part has an **Edit** button. Your edit is saved to the story the asset came from (the
same edit as the Cast and Sets tabs), and the library follows. **Redraw out-of-date** redraws
just this character's or set's out-of-date pictures, then what is drawn from them
(`POST …/images/refresh` takes `requirement_ids` to limit it).

### Reference images

On a finished package, **Generate images** (Assets tab, or `POST
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
  about 1024 px images, much faster. Progress is live in the Assets tab; interrupted images
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
| `GEMINI_API_KEY` / `GEMINI_URL` | — / `https://generativelanguage.googleapis.com/v1beta` | enables Nano Banana as a second keyframe provider (`nano-banana`); a gateway serving the Gemini API can stand in for the URL. With fixture images, a local stand-in is offered instead |
| `GEMINI_IMAGE_MODEL` | `gemini-nano-banana-2.1` | the Gemini image model for keyframes |
| `DRAMA_KEYFRAME_PROVIDER` | `h3` | the keyframe provider when a request names none (`h3` or `nano-banana`) |

The image route needs an MCPHub key from a `newsbreak.com` account; it is sent as
`X-API-Key`.

### Casting & design: takes, draws and picks

The Assets tab's **Casting & design** board explores several designs per item before
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
picture per cut, so characters stay consistent and each cut starts as planned:

1. **Plan the cuts**: the text model splits each shot's clip into 1-3 sub-shots, each with
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
the Experiment tab's restrictive mode: the picks are the production's chosen designs, so
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

**The shot specs are the contract for video production.**
`03_storyboard/shot_specs.json` (`spec_version` 2) gives, per shot, everything dramer makes
the clip from, so dramer stores and sends it instead of working it out again:

- `spec`, in dramer's shape: `summary`, `action`, `camera`, `lighting` (from the set sheet),
  `target_duration_ms` (the plan's clip, or the storyboard's length for an unplanned shot),
  `dialogue` (each line once: a line that runs over a cut is spoken in the shot it starts in,
  and the next shot's `continuity_notes` says it carries over), `sound_notes`,
  `continuity_notes`, and `sub_shots` (the cuts below, each `keyframe` a path, each `now` the
  people's states; null for a shot nobody planned that has no keyframe);
- `references`: the pictures to send, in priority order, each with `file` (a path from the
  export's root), `kind` (keyframe, chosen, look, face, set, prop), `entity_id`, `name`,
  `sub_shots` (the cuts a keyframe or a chosen picture is for) and `likeness`. A shot whose
  every cut has an approved keyframe is `keyframe_first`: its keyframes, then the faces (to
  hold each likeness), the props, each person's picture and the set. Any other shot: its
  keyframes, each person's picture (the reading's choice, else the storyboard look), the set,
  the props, the pictures chosen for later cuts, then the faces. Keyframes are references the
  video matches, not start frames. A consumer with a smaller limit than `max_references`
  drops from the end;
- `cast`: each person, the place and each prop with a note (a person as the reading found
  them in their first cut, else their storyboard look);
- `ready`, and `not_ready` saying why not: the plan's problems, each cut with no approved
  keyframe, and any line the shot list cues that the script no longer has;
- `spec.carried`: the lines spoken in the shot before that run over the cut into this one
  (also noted in `continuity_notes`).

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
- every on-screen person has a side of the frame, and only they do;
- every cut changes the view (no two sub-shots in a row share a framing);
- screen sides hold across cuts (the 180-degree rule): nobody jumps from frame left to
  frame right, on screen or off.

The planner covers dialogue speaker by speaker (a cut to the speaker shortly before their
line) unless the storyboard asks to hold a two-shot.

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

- **Content rating** (job option `content_rating`, under Options): `general` allows at most a
  kiss; `teen` (default) at most *implied* intimacy (the scene cuts away), no nudity;
  `mature` allows simulated intimacy with at most partial nudity, on a closed set.
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

### Experiment: chat with the image model

The **Experiment** tab (top bar) is a chat for trying MiniMax-H3-Image by hand. Chats are
saved and listed on the left.

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
   writes one prompt, never a grid of options in one image.

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

The **Face Lab** tab turns a photo of a face into the most descriptive text prompt GPT can
write, then tests that prompt by itself, so you can see how close words alone get, and keep
a description that works as a reusable, castable character prompt.

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
(`MCPHUB_MODEL_SHOT_PLAN`, `_SHOT_FRAMES`, `_SHOT_CHECK`, `_SHOT_READING`; reading a scene
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
  validation/      Deterministic hard checks (spec §8) and ID index
  llm/             Vendor-neutral ModelClient protocol, MCPHub client, offline fixture
  config.py        .env loading and model client selection
  prompts/         Base orchestrator prompt (verbatim from spec §10)
  store/           Repository protocol; SQLite (default) and in-memory backends
  jobqueue.py      Durable job queue (leases, retries, checkpoints)
  media.py         Uploaded images/videos: storage, type sniffing, keyframes, audio
  imagegen.py      Image generation clients (MiniMax-H3-Image on MCPHub, offline fixture)
  images.py        Reference image planning, prompts, references and the image worker
  experiment.py    Experiment tab: image chat sessions, the GPT prompt rewriter, the runner
  face_lab.py      Face Lab: photo-to-prompt descriptions and seeded prompt tests
  content_guard.py What image prompts may ask for (word lists + a model screen)
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
- **Revisions are immutable**, except for hand edits to sheet entries, which change a
  revision in place. Packages are stored per revision; a stale `base_revision_id` returns
  409.
- `RevisionRequest.visual_change` is an addition to the spec. It says whether an edit to a
  character/location/prop changes how it looks (which invalidates art requirements) or
  only changes story facts.

## Not implemented yet

- Script files as uploads (`type: script` with an `asset_id`); paste scripts as text.
- Non-speech audio analysis (music, sound effects) for videos.
- The revision regeneration worker: `POST .../revisions` validates the request, runs the
  impact analysis and records the revision as `queued`, but nothing regenerates content yet.
- Upstream retry: a failing step only retries itself. It never re-runs earlier steps.
