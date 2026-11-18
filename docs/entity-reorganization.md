# Draper around Story, Characters and Sets: a design

18 Nov 2026. A proposal for review: nothing in it is built yet.

## Goal

Draper's job is to turn stories into a library of high-quality **character assets** and
**set assets**, and to keep that library growing: each new episode or story reuses what is
there and adds what it needs. Everything else draper does (script, storyboard, cut plans,
keyframes, export) is the story's *use* of those assets, and the check that they hold up.

Decisions taken:

| Question | Decision |
| --- | --- |
| Library scope | **Across stories.** A character or set is its own entity; any number of stories use it, and it outlives story revisions |
| Props | **Split by use.** Set dressing belongs to the set; what a person carries or wears belongs to the character's looks and states (as the scene reading already tracks) |
| Shots and keyframes | **Inside Story.** The storyboard, cut plans, keyframes and readings are the story's use of the assets; each asset shows the shots that use it |
| First step | This document, then phased work |

## Today: organized by pipeline step

The UI runs Overview, Script, Storyboard, Cast, Sets, Props, Assets, Keyframes, QA, Revise and
Activity: one tab per pipeline step. One character is spread across four places:

| Fact about Lila | Where it lives now |
| --- | --- |
| Who she is, her face, her default wardrobe | `cast_sheet.characters` (Cast tab) |
| Her morning look's wardrobe and hair | `cast_sheet.looks` + `story_bible.looks[].description` |
| Her face and look pictures, takes, picks | `generated_images`, `design_options` (Assets tab) |
| How she looks in each cut, which picture fits | `scene_readings` (Keyframes tab) |

A set is spread the same way (Sets tab, story bible states, Assets, keyframes' set
references). Props have their own tab, though most are either set dressing or something a
person holds.

How things are stored:

- **Everything is keyed by `(package_id, revision_id)`:** `packages` (the whole package as
  one JSON document), `generated_images`, `design_options`, `shot_takes`, `shot_prompts`,
  `shot_plans` and `scene_readings` (SQLite schema 11).
- **An asset's identity is its requirement id**, derived from story ids:
  `ar_character_look_ch_lila_lk_lila_morning`. It exists only inside one package revision.
- **Revisions don't carry assets.** `POST …/revisions` works out the impact, but its
  regeneration step is a TODO. A new story never sees another story's assets.
- **The only exception:** hand edits (`sheet_edit`) change a revision in place.

Code:

- `shot_frames.py` is 2,600 lines: plans, references, takes, the runner, the judge, the
  board and staleness.
- `images.py` is 1,400 lines: design slots, prompts, takes, refresh and the worker.
- `api/app.py` is 1,400 lines.

## The model

### Character asset

```
Character            chr_<id>      name, sheet (face, build, hair, distinguishing features,
                                   default wardrobe, palette, must_keep, design_freedom)
  identity           the face anchor picture; likeness (real person or designed); uploads it came from
  looks[]            look_<id>     wardrobe, hair and makeup, condition, one-line description
    pictures         full body (pick + options), and any extra views
    carried[]        items worn or held in this look (shoes, bag, business card), each with
                     an optional detail picture
  states_seen[]      from scene readings: wearing / hair / carrying / condition per cut
  history            hand edits (with time), picks, redraws
  used_in            stories → shots → keyframes
```

### Set asset

```
Set                  set_<id>      name, sheet (layout, furnishings, materials, palette,
                                   entrances and blocking, ambient sound, must_keep, design_freedom)
  states[]           state_<id>    time of day, lighting, dressing, condition, one-line description
    pictures         wide view (pick + options), plan view, extra angles
    dressing[]       set props in this state (the painting, the console, scattered buttons),
                     each with an optional detail picture
  history, used_in   as for characters
```

### Story

```
Story                story_<id>    source, bible (synopsis, arcs, episodes), script, storyboard
  casting            story character id → Character + the looks this story uses
                     story location id  → Set + the states this story uses
  shots              cut plans, prompt versions, keyframe takes, approvals, scene readings
  revisions          story text changes; casting and assets are not copied, they are referenced
```

**Casting is the hinge.** The pipeline still writes a story's bible and sheets as it does
today. A casting step then matches each story character and location to a library asset, or
creates a new one. What the story asks for and the asset lacks (a new look, a new set state)
becomes an **expansion**: proposed text, drawn from the asset's anchors, reviewed like any
design. That is how the library keeps growing.

**The library is canonical.** A sheet edit on Derek's apartment changes the set every story
sees. A story that needs the room different (redecorated in season 2) adds a new state, or
forks the set when it is really a different place. The out-of-date machinery built on
18 Nov already works per picture and per design text, so a library edit flags every
keyframe that used the old version, in every story.

### Props, split by use

| Prop | Belongs to | Example |
| --- | --- | --- |
| Set dressing, stays in the room | Set state `dressing[]` | the abstract canvas, scattered buttons |
| Worn or carried, moves with a person | Character look `carried[]`, tracked per cut by the reading | Lila's pumps, her work bag, the business card |
| A large object that is a place in itself | A set | Derek's restored vintage car, if scenes play in or beside it |

The scene reading already decides "carrying: one shoe" per cut. Moving carried props onto the
character means their pictures are chosen the same way as the person's.

## Storage

Schema 12 adds library tables beside today's, keyed by asset ids, not by revision:

| Table | Holds |
| --- | --- |
| `characters`, `character_looks` | Sheets and look text, with design history |
| `sets`, `set_states` | Sheets and state text, with design history |
| `asset_images` | Each picture's owner (`character`, `look`, `set_state`, `carried`, `dressing`), its view, its pick flag and its brief. Today's `generated_images` rows move here with their ids unchanged |
| `story_casting` | Story entity id → asset id, plus the looks and states the story uses |

**Kept:** `packages` (now only story text), `shot_*` and `scene_readings` stay keyed by story
and revision. Keyframe takes keep referring to picture ids, which don't change.

**Migration:** for each package's latest revision, create assets from its sheets and picks,
write its casting, and point its images at the new owners. Old revisions stay readable. The
Heartbeats keyframes keep working, because picture ids are kept.

## UI

The top bar has **Stories · Characters · Sets**, plus Experiment and Face Lab as tools.

- **Story page:** Overview, Script, Storyboard, Casting, Keyframes, QA, Export. Casting shows
  each story character or location, the asset it's cast as, the looks and states used, and
  any proposed expansions.
- **Character page:**
  - the sheet (edit in place), identity and likeness;
  - each look with its picture, options and carried items;
  - the states seen across stories;
  - where it's used (stories, shots, approved keyframes);
  - out-of-date marks and "Redraw out-of-date".
- **Set page:** the same for sheet, states, views, dressing and usage.

Today's Cast, Sets, Props and Assets tabs fold into the Character and Set pages.

## Code layout

```
src/drama_agent/
  library/      store and models for characters, sets, pictures, casting; sheet_edit;
                out-of-date rules for pictures (today's images.outdated_pictures)
  characters/   identity, looks, carried items: design slots, prompts, takes, develop
  sets/         states, views, dressing: design slots, prompts, takes, develop
  story/        pipeline (bible, script, storyboard), casting, revisions
  shots/        plan.py, references.py, takes.py, runner.py, judge.py, reading.py,
                staleness.py (today's shot_frames.py and shot_reading.py, split)
  export/       package zip, shot_specs, sub_shots (unchanged output)
  api/          one router per area; today's paths kept as aliases during migration
```

## The dramer contract stays

Export flattens a story's cast and sets into today's package zip, with the same story ids,
`shot_specs.json`, `sub_shots.json` and reference images. Dramer needs no change. Later,
dramer could import a library asset once and reuse it across projects. That's a separate
decision.

## Phases

| # | Work | Visible result | Size |
| --- | --- | --- | --- |
| 1 | Library tables, migration from existing packages, read paths; export unchanged | Nothing yet; every test and Heartbeats still work | medium |
| 2 | Character and Set pages: sheet editing, looks or states, pictures, out-of-date and redraw, usage; Cast, Sets, Props and Assets fold in | The new navigation | large |
| 3 | Casting step: match a new story to the library (a person confirms), propose expansions, draw them | A second story reuses Lila or the bedroom | medium |
| 4 | Split `shot_frames.py`, `images.py` and `api/app.py` along the new layout | Smaller modules, no behaviour change | medium |
| 5 | Story revisions reference assets instead of copying (the revision TODO) | Revising the script keeps every asset | medium |

Each phase is shippable on its own, with draper and dramer working throughout.

## Phase 1 as built (18 Nov 2026)

- **Code:** `src/drama_agent/library/` (models, `sync`, read views), API in
  `api/library.py`, schema 12 (`library_assets`, `story_casting`, `asset_images`), tests in
  `tests/test_library.py`.
- **Pictures are linked, not moved.** `asset_images` links each `generated_images` row to
  the asset it shows. Rows and ids stay where they are, which is safer than moving them now.
  Moving them can wait until something needs it.
- **The library mirrors the stories.** Each story's latest revision is synced into it at
  startup and before every library read. A story unchanged since its last sync is skipped.
  Edits still happen on the story (sheet_edit) and flow into the library. The library
  becomes the original you edit in phase 2.
- **One asset per story entity.** Ids are derived from the story and its ids, so a resync
  finds the same asset. Matching across stories is phase 3's casting.
- **Props, split by use:** an owner in the story bible makes a prop that character's item;
  otherwise it dresses the set it appears in most, by shot count.
- **Read API:** `GET /v1/library/characters[/{id}]`, `GET /v1/library/sets[/{id}]` (each with
  its looks or states, items, current pictures, and the stories and shots that use it; lists
  carry a `cover`), `GET /v1/drama-packages/{id}/casting`, `POST /v1/library/sync`.
- **On the live database:** 7 stories gave 30 characters, 17 sets and 452 linked pictures.
  Opening it upgrades it to schema 12, and older draper code refuses a newer schema.

## Phase 2, step 1 as built (18 Nov 2026)

- **The pages:** Characters and Sets in the top bar (`web/library.js`, `#lib=characters`,
  `#lib=sets`, `#lib=<asset id>`). Each shows a grid with covers and a filterable sidebar.
  An asset's page has its sheet, looks or states, and items. Each part shows its pictures
  (enlarge on click; `outdated` reasons from the design-edit rules), and the stories and
  shots that use it, with links.
- **Step 2 (built):**
  - **Editing:** each character, look, set, state and item has an Edit button. Every sheet
    field can be edited, plus the one-line description for looks and states. Only the fields
    that changed are saved, to the story the asset came from (`PATCH …/entries/{id}`), and
    the library follows.
  - **Redraw out-of-date:** redraws only this asset's out-of-date pictures
    (`POST …/images/refresh?requirement_ids=…`). The pictures drawn from them follow, and the
    page reloads itself until it's done.
- **Step 3 (built):**
  - **Casting tab:** the story's Cast, Sets, Props and Assets tabs became one **Casting** tab.
    It has the story-wide Explore and Develop controls, the style brief, and the story's
    characters and sets as cards (cover, counts) linking to their library pages. Props
    show who carries them. Old `tab=cast|sets|props|assets` links land on Casting.
  - **Design board per asset:** each face, look, state and item on a library page has
    **Takes and draws**, using the board's own functions (`renderTakeRows`, `renderNewTake`).
    The board's loading, actions and polling now redraw whichever is open (`designOpen`,
    `designRefresh`), and a library page reloads its asset after each action.
  - **Removed:** the old tabs' code (the sheet tabs and their editor, `renderAssets`,
    `renderDesignBoard`, `editTextLinks`), about 300 lines.

## Phase 3, step 1 as built (18 Nov 2026): matching and linking

- **Matching** (`library/matching.py`): for each of a story's characters and sets, assets of
  other stories with the same name (width-folded, so full-width brackets match), one name
  containing the other, or the same first name, best first. This step matches by name only:
  it is free and predictable. A person confirms.
- **Linking** (`POST …/casting {story_id, asset_id}`; `DELETE …/casting/{story_id}` undoes it):
  - **The story points at the shared asset:** the story's casting points at the other
    story's asset, marked `source: "user"`, and every sync keeps it.
  - **Its own copy is hidden:** it's marked `merged_into` and left out of the lists.
  - **Its looks or states join the shared asset:** its looks or states (and items) hang off
    the shared asset, so the library grows. They're still used by their own story's shots.
  - **Its pictures stay visible:** they show on the shared page under "Also drawn in".
- **Checks:** a link must be to the same kind of asset, from another story, not itself
  linked elsewhere, and not from an archived story.
- **Archived stories:** stories whose jobs are all archived (the pre-English-only Chinese
  stories) are kept in the library but not shown or offered, like the stories themselves.
- **UI:**
  - **Casting tab cards:** each offers "Same as <name> in <story>? Link", or shows "Shared with
    <story>" with Unlink.
  - **Library pages:** they load each part's own story's design board, so a linked story's look
    keeps its takes ("in its own story"), and each part's edits go to its own story.

## Phase 3, step 2 as built (18 Nov 2026): reuse

- **What it does** (`library/reuse.py`, `POST …/casting/{story_id}/reuse`): a linked story
  takes the shared asset's current anchor pick, the face of a character or the main wide view
  of a set (its first state).
- **How:** it's copied in as an imported pick, like an Experiment image, and the story's old
  pictures stay among its takes.
- **What follows:** everything the story drew from its old anchor (a character's looks, a
  set's other states and plan views) goes out of date. "Redraw out-of-date" on the shared
  page redraws it from the shared picture, so the stories draw the same face or room.
- **UI:** a linked Casting card has **Use its face** or **Use its set view**. It says how
  many pictures went out of date and links to the shared page.
- **Next:**
  - **Matching looks:** match looks and states that duplicate each other (the same "Apron" look
    in both stories).
  - **Suggestions:** propose matches when a new story is made, matched by the model, not only
    by name.

## Phase 4 as built (18 Nov 2026): the big files split

This changes no behaviour. The split was done by script, and each new module's imports were
derived from what it uses, with any import cycle refused.

- **`shot_frames.py` (2,600 lines) became `shots/`:**
  - the data: `models`, `common`;
  - shot planning: `picks`, `plans`, `approvals`, `staleness`, `references`, `takes`;
  - the model prompts: `writer`, `planner`, `judge`;
  - running and showing it: `runner`, `board`.
- **`images.py` (1,400 lines) became `design/`:** `prompts`, `slots`, `staleness`, `takes`,
  `worker`, `setup`.
- **Old names still work:** the old modules re-export every name, including names other
  files took from them that they had only imported, so existing imports keep working.
- **`api/app.py` (1,400 lines) is now about 290:**
  - its request and response models went to `api/schemas.py`;
  - its route groups became `designs`, `experiments`, `jobs` and `packages`, each a
    `register(app, ctx)`;
  - helpers used by more than one group stay in `create_app`;
  - `packages` registers last, since its `/{package_id}/{sheet}` route catches what the
    others don't (the same 79 routes, and no overlapping pair changed order);
  - one test now patches the export folder in `api/schemas`.
- **Done in a git worktree:** splitting in place took the live server down twice.

## Risks and open questions

1. **Same name is not the same character.** Casting across stories must be confirmed by a
   person; the model only proposes matches.
2. **Canonical versus per-story variation.** When does a change become a new state, and when
   a fork? The proposal is: a state when it's the same place at another time or dressing, a
   fork when it's a different place.
3. **Likeness travels with the asset.** A face from an uploaded photo keeps its rule-3 flag
   in every story. Intimate scenes stay refused for it everywhere.
4. **Rights to imported pictures.** An asset made from an uploaded photo (the bedroom) carries
   where it came from, so reuse can be checked.
5. **Cost of a canonical edit.** Editing a shared set flags keyframes in every story that uses
   it. The board should show the blast radius before saving.
6. **Migration of old revisions.** Only each package's latest revision is migrated. Older
   ones stay read-only, as they are.
