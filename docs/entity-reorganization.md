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
