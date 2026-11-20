# Draper's functions in dramer: a plan

19 Nov 2026. A plan for later: nothing in it is scheduled or built.

## Goal

Video production (dramer, `ParticleMedia/dramer`) takes over what draper does, so one app
goes from a seed to a final cut. This is a **function transfer, not a code transfer**: each
draper function is rebuilt in dramer's own stack (TypeScript, MySQL through Kysely, worker
jobs, React pages). No Python code moves.

Until an area is rebuilt, draper keeps doing it and dramer's `importPackage` keeps carrying
its results across. When the last area lands, draper is retired.

## The two apps today

| | draper | dramer |
| --- | --- | --- |
| Does | seed → story bible → scripts → storyboard → cast/set/prop sheets → reference images → shot keyframes → export | import → video prompts → clips → voice, music, subtitles → review → final cut |
| Stack | Python, FastAPI, SQLite, one process, static JS UI | TypeScript, Fastify, MySQL, API plus job workers in lanes, React UI |
| Size | about 28,000 lines | about 28,000 lines |
| Hand-off | writes `03_storyboard/shot_specs.json` and `sub_shots.json` | reads them with `importPackage` |

Both already have a Nano Banana client, an H3 image client and image prompt rules.

## What dramer has and what it lacks

Dramer already has simple versions of the early steps: `generate_story`, `generate_script`,
`generate_storyboard`, `extract_assets`, reference images (H3 stills or Nano Banana),
character sheets and looks. Draper's versions go much deeper. Video, voice, music, review,
checks and the cut stay as they are in dramer.

| Area | In draper | In dramer today |
| --- | --- | --- |
| **A. Pictures and library** | Characters, sets and props across stories; looks, states and layers; takes, draws and picks; anchors and parents; marking pictures out of date and redrawing in order; the H3 image prompt rules; the content screen | A simpler Assets page: sheets, looks, an image picker, the image model picker |
| **B. Develop in a chat, Face Lab, studies** | Image chat sessions, the GPT prompt rewrite, Drawn with H3 or Nano Banana, photo-to-prompt, seeded tests | None |
| **C. Shot keyframes** | Sub-shot plans, scene reading, keyframe takes, judge, approval, readiness, shot specs | Only what `importPackage` brings in |
| **D. Story pipeline** | Seeds from text, photo or video; story bible; scripts; cast/set/prop sheets; timed storyboard; asset requirements; hard checks with bounded retries; Revise with impact and locks; live trace with pause and resume | One step per page, without the checks, revisions or trace |

## Order

1. **A (pictures and library) first.** It is where most of the current work happens, and B
   and C build on it. Dramer's Assets page grows into draper's library.
2. **B (develop in a chat).** It needs A's pictures, slots and picks.
3. **C (shot keyframes).** Once this lands, keyframes and shot specs are made in dramer
   itself, so most of `importPackage` goes away.
4. **D (story pipeline).** It replaces dramer's simple story, script and storyboard steps.
   It goes last because dramer already has something working there.
5. **Retire draper.** One last import of each draper story, bringing its pictures, library
   and keyframes. Then draper is frozen.

Each area ships on its own and is usable right away, so the work can stop after any phase.

## How each area is rebuilt

- **Draper's tests define the work.** Before an area is done, each feature's draper tests
  get a dramer test (vitest) of the same behaviour. Wording and screens can change; what the
  feature does can't.
- **Dramer's building blocks.** New tables are Kysely migrations. Model calls and image draws
  are worker jobs: images in the `image`/`still` lanes, text in the `text` lane. Pages are
  React.
- **An offline stand-in model.** Dramer gets a fixture stand-in like draper's, so tests and
  trial servers never call paid models.
- **One way to call each model.** Text goes through dramer's `anthropic`/`mcphub` adapters;
  draper's GPT-on-MCPHub calls become `mcphub` jobs. Images go through `gemini` and H3.
  Dramer's H3 image calls go to a self-hosted still server (`H3_STILL_URL`); draper uses H3
  through MCPHub's GPU route, which dramer gains as one more adapter.
- **Same rules.** English only, and the H3 image rules (the `minimax-h3-image-prompt` skill)
  move to dramer's `.claude/skills`.

## Size

Weeks of work, not days. A is the largest area; D needs the most care because of the checks
and revisions.

## Open questions

1. **Freeze draper during the rebuild?** Features added to draper meanwhile have to be
   rebuilt twice. Suggestion: fixes only in draper, new features in dramer.
2. **Order:** A → B → C → D, or D first so a whole story can be made in dramer sooner?
3. **What to drop instead of rebuilding:** for example draper's own web pages that dramer
   already covers, Face Lab, or the Markdown/JSON package export.

## First step when this starts

A checklist for area A, built from draper's tests, before any code is written.
