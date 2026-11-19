# The workbench: Experiment and Face Lab inside Characters and Sets

Status: built (19 Nov 2026), phases 1 to 3.

## Why

The Experiment tab (image chats with MiniMax-H3-Image) and Face Lab (a photo turned into a
text prompt, tested seed by seed) exist to develop character and set pictures. Today they
are separate top-level tabs, with no tie to the story they work for:

- a chat or study doesn't say which character, look, set or state it is for;
- a picture made there reaches a story only through an API call
  (`POST /v1/drama-packages/{id}/images/import`); no button in the UI does it. Derek's
  redecorated apartment came in that way.

The engine already fits the job:

- chats keep a current image and write prompts (rewrite or exact);
- they send references, track likeness and screen with the content guard;
- they retry, and share the GPU queue with keyframes.

The story side already handles a new pick: what was drawn from the old one goes out of
date, and **Redraw out-of-date** redraws it in order (docs/entity-reorganization.md). So the
change is in the UI and in linking the two together, not in how images are made.

## The idea: a workbench on each picture

Every picture slot on a character or set page gets **Develop**:

- a character's face and each look;
- a set's first wide view, its other states, and the plan views.

It opens a chat tied to that slot.

- **What it starts from:**
  - the slot's current pick, as the image the next message changes;
  - the prompt that made it, which the rewriter builds on;
  - its likeness mark.
- **What it does:** everything a chat does today.
- **Use as pick:** each result has this button. It makes the result the slot's pick, with
  the import code that exists. The usual rules then flag what was drawn from the old pick,
  and Redraw out-of-date refreshes it. Old picks stay among the slot's takes, so any change
  can be undone by picking one again.
- **The slot's chats** are listed under it, newest first.
- **Face Lab becomes "From a photo"** on a character's face slot: describe a reference
  photo, test the wording with fixed seeds, then use a render as the face pick (or the
  wording as its prompt).

## Model

`ExpSession.target` (optional) says what a chat or study is for:

| field | |
| --- | --- |
| `package_id` | the story |
| `requirement_id`, `deliverable_id` | the picture slot (as in `generated_images`) |
| `label` | what to call it ("Derek Blackwood, face"; "Derek's apartment, morning") |

The slot is named by the story's requirement and deliverable ids. They are stable across
revisions, so a chat follows a story through a revise run. A chat's pictures go to the
story's latest revision.

**Starting from the pick** is a first turn of a new mode, `pick`. It draws nothing; its one
result is a copy of the pick's file, numbered #1 like any result. The turn's prompt is the
pick's prompt and its likeness is the pick's. So the rest of the chat needs no special
case: "make the window bigger" is rewritten from the prompt that made the picture, and the
content rules see the same likeness mark the story does.

## API

- `POST /v1/experiments` takes `target` and `start_from_pick` (default true). The title
  defaults to the target's label. An unknown story, requirement or deliverable is a 404.
- `GET /v1/experiments?package_id=&requirement_id=&deliverable_id=` lists a slot's chats.
- `POST /v1/experiments/{id}/use` with `{image: N}` makes the chat's #N the pick for its
  target, the same as the import endpoint, and returns the new design image.

## Phases

1. **Link and use** (this phase; the top tabs stay):
   - the target on chats and studies;
   - the `pick` start;
   - Use as pick on a targeted chat's results;
   - Develop and the slot's chats on the character and set pages (Develop opens the chat
     in the Experiment tab for now).
2. **The workbench on the page:** the Experiment and Face Lab views become a side panel
   over the character or set page (reusing `experiment.js` and `face.js`). Develop and From
   a photo open it in place, so the sheet and the other looks stay in view.
3. **Retire the top tabs:**
   - Experiment and Face Lab leave the top bar;
   - chats tied to nothing (the existing ones, and free experiments) live under **Scratch**
     on the library pages;
   - old `#exp` / `#face` links keep working.

## Phase 1 as built

- **Develop in a chat** sits above each picture's "Takes and draws" on the character and
  set pages (a face, each look, each set state's views). The picture's chats are listed
  beside it. It opens the chat in the Experiment tab, starting from the pick (#1).
- A targeted chat says what it is for under its title, with a link to the story. Every
  result has **Use as pick** (`POST /v1/experiments/{id}/use`). The import endpoint and Use
  as pick share one code path (`_use_chat_image` in api/designs.py).
- A slot with no pick yet starts an empty chat (text to image). A Face Lab study can carry a
  target, but nothing uses it yet (phase 2).

## Phase 2 as built

- **The panel** (`web/bench.js`): Develop in a chat, a picture's listed chats, and From a
  photo open over the character or set page instead of leaving it. The panel has Open full
  page (the Experiment or Face Lab tab) and Close (or Escape). On a wide window (1400px
  and up) the page makes room for it.
- **The views mount into the panel.** `experiment.js` and `face.js` draw into a host
  element (`exp.host` / `face.host`) when one is set, and into `#main` otherwise. They
  scroll the panel, not the page. A poll that lands after the panel closed finds its host
  detached and draws nothing.
- **From a photo** sits next to Develop on a character's face. It uploads the photo,
  starts a study for that face and has the photo described. Each render then has Use as
  pick.
- After Use as pick, and when the panel closes, the page reloads its view: the new pick,
  what is now out of date, and the picture's chats.
- Leaving the page (any route change) closes the panel.

## Phase 3 as built

- **The top bar** is Drama, Characters and Sets. The Experiment and Face Lab tabs are gone.
  Their full pages stay at `#exp=<id>` / `#face=<id>`, the target of the panel's Open full
  page, and old links keep working.
- **Scratch**, in the Characters and Sets sidebar, lists every chat and study tied to no
  picture, newest first. **New chat** and **New study from a photo** start one. Each opens
  in the panel.
- **Link a scratch chat…** on a picture's row links one to that picture
  (`PATCH /v1/experiments/{id}` with `target`). A face study can only be linked to a face.
  The chat leaves Scratch and is listed under the picture. Linking doesn't add the pick to
  the chat. **Unlink**, in a linked chat's or study's "For …" line, sends it back to Scratch
  (`{"unlink": true}`), so a wrong link is easy to undo.
- A face study can be for a character's face only. The server refuses anything else (422),
  since its Use as pick would otherwise replace another kind of picture with a face.
- The restrictive-mode switch says it applies to image chats.

## Chat history (each chat kept)

- **Every chat is saved**, each with its own turns, and a picture's chats are listed at the
  top of the panel (**Chats for this picture**): title, message count, last activity,
  newest first. Click one to continue it; **New chat** starts another from the picture's
  current pick, and the one you were in stays as it was.
- **Develop in a chat**, clicked again before anything is written, reopens that untouched
  chat instead of making another (`reuse_empty` on `POST /v1/experiments`). A chat counts
  as untouched while it holds only its pick start (`ExpTurn.from_image`, the pick it
  copies) and that pick is still the picture's pick.
- **A chat is titled by its first message** (as Scratch chats are), unless it was renamed;
  until then it carries the picture's name. `GET /v1/experiments` gives each chat's
  `messages` (the pick start is not one).
- **Chat images are about 4 MP ("2K")** for quick iteration: 2048x2048, 1728x2304 for
  3:4 and so on (`experiment.SIZES`). A size saved before (the earlier 16 MP ones) draws at
  the same aspect ratio's 2K size.

## Choices

- **A side panel, not a full page** (phase 2): the sheet, the other looks and the out-of-date
  flags stay visible while you work.
- **Free experiments stay** under Scratch: some are for no character yet (a casting idea).
- **The existing chats stay unlinked**; a chat can be linked to a slot later (phase 3).
- **Use as pick doesn't redraw by itself.** The page shows what is now out of date and
  offers Redraw out-of-date, as after any new pick.
