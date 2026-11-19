# Characters and Sets in layers

A character or set page opens in three layers, each a link with a breadcrumb back:

```
Characters › Lila Montgomery › Look: Evening torn › full body
Characters › Lila Montgomery › Look: Evening torn › Shot sh_02_01_05 · keyframes
#lib=chr_…     (the character)
#lib=chr_…&part=look_…             (one look, state or item; the character itself for its face)
#lib=chr_…&part=look_…&slot=d_…    (one picture: its takes)
#lib=chr_…&part=look_…&shot=sh_…   (one shot it is in: its keyframes, to review and develop)
```

1. **The character or set:** its line, its sheet (folded; Edit in place), where it's used,
   and its looks or states and items as cards. A card shows the picture's pick, the look's
   line, its number of takes and of shots (or "in no shot yet"), and "out of date". A
   character's first card is its **Face**. **+ New look** (or **+ New state**) is here.
   Other stories' pictures of a linked character stay under "Also drawn in".
2. **A look, state or item:** its line and sheet (Edit), the shots that use it, a card per
   picture (a look's full body; a state's wide and top-down views), each with its pick and
   number of takes, and its **Keyframes**: a row per shot it is in, with each cut's
   approved keyframe (solid green) or newest draw (dashed), rounds, "out of date" and
   "drawing…".
3. **A picture:** the **Current pick**, large, at the top (which take and draw, or where it
   was brought in from; dashed "No pick yet" showing the newest draw when nothing is
   picked), the New takes controls (draws, draft or full size), the workbench row (Develop
   in a chat, From a photo, Create from text, its chats), its **Takes** (the one holding
   the pick says so), what was **Brought in**, and **New take** (or what must be picked
   first).
4. **A shot** (from a look's or state's Keyframes): the Keyframes tab's shot card, with the
   draws, rounds and provider for new takes. See "Developing a keyframe" below.

Moving between layers of the page on screen shows what is loaded at once and refreshes it
behind. The workbench panel closes on each move, as on any page change.

## Brought in

A picture's design images that belong to no take: a keyframe draw sent to it, a chat
image made its pick ("Use as pick"), or a draw from before takes. Each says where it came
from and links there (the story's Keyframes tab, the chat). Click one to make it the pick.
Before this, a chat image used as a pick showed only as the current picture.

## New look, new state

`POST /v1/drama-packages/{id}/variants {parent_id, description, sheet, revision_id}` adds,
in place (as a hand edit does, logged in the change log): the story bible line
(`CharacterLook` / `LocationState`, in no scene), its sheet entry (`CastLook`: wardrobe, hair
and makeup, condition; `SetState`: time of day, lighting, dressing changes, condition) and
the asset requirement its pictures are drawn for, built from the sheets as the pipeline
builds them (`sheet_edit.add_variant`). A look depends on the face, so it's drawn from the
face's pick; a character with no face picture in the story gets no new look (422). Its id
comes from the line ("lk_lila_rain_soaked_gala"). The page opens on its picture, ready for
New take.

No shot wears it yet, so its requirement's `used_by` is empty (allowed only for these).
A script revision that puts it in scenes uses it; one that doesn't may drop it, with its
pictures, as it may drop any look the script no longer has.

## A keyframe as a take

A finished keyframe draw has **Send to a look…**: the looks its shot's characters wear and
the shot's set state (wide view), in the story's latest revision.

- `GET /v1/shot-takes/{take}/draws/{n}/design-targets`: each target, its page in the library
  (`lib_asset_id`, `part_asset_id`), and whether this draw was sent there (`sent_image_id`,
  `picked`).
- `POST /v1/shot-takes/{take}/draws/{n}/to-design {requirement_id, deliverable_id, pick}`:
  the draw is copied in as a design image of that picture (`imported_from` "keyframe
  <take>:<n> (shot <shot>)"), shown under Brought in. It becomes the pick only with `pick`
  ("Add and pick", or later "Make it the pick"). Sent again, the same copy is used. The
  copy outlives the keyframe.
- Its likeness is the keyframe's; in a story with uploaded photos an unmarked keyframe
  counts as a likeness, as a chat image does.
- Making it the pick is a pick like any other: keyframes drawn from the old pick go out of
  date.

## Developing a keyframe

Keyframes are shot-specific: a look's page lists them (its Keyframes), but they are never
its takes. A look's takes are candidates for the look itself; a keyframe's rounds are tries
at one cut of one shot, with the other people, the set and the framing. "Send to a look…"
stays the one way to make a keyframe a take of a look.

Each cut (sub-shot), on the shot's page and on the story's Keyframes tab, has:

- **The approved keyframe, large,** at the top: which round and draw (or which chat image)
  it is, and its check. With none approved, the newest finished draw, dashed.
- **Develop in a chat:** a chat with the image model starting from the approved draw (else
  the newest), opened in the workbench panel; each draw also has its own **Develop in a
  chat**. The cut's chats are listed beside the button. Clicked again before writing
  anything, the same chat opens. Mention a look's or set's `@number` in the chat to bring
  its picture in.
- In the chat, **Use as keyframe** on any image brings it back as a take of that cut
  ("From a chat", linked to the chat), approved, then checked like any draw. The later
  cuts' approvals drawn from the old keyframe are withdrawn, as with any new approval.
- Rounds, approve, withdraw, check again, prompt versions and New take work as on the
  Keyframes tab.

API:

- A chat's target is either a design picture (`requirement_id`, `deliverable_id`) or a
  keyframe (`shot_id`, `sub_shot`): `POST /v1/experiments {target: {package_id, shot_id,
  sub_shot, label}, from_frame: "<take_id>:<n>", reuse_empty}`. Its first turn is a copy of
  the draw (`from_image` "frame:<take>:<n>"), with the keyframe's likeness (an unmarked one
  in a story with uploaded photos is judged from the picture). `GET /v1/experiments?shot_id=`
  lists a shot's chats. Use as pick refuses a keyframe chat.
- `POST /v1/experiments/{id}/use-keyframe {image, approve=true}` makes a `ShotTake` with
  one draw, the chat's image (`imported_from` "experiment <id> #<n>"). It copies the
  newest take of that cut drawn for the plan in force (references, design print, size), so
  it stands where a drawn keyframe stands: approved, exported, and drawn from by the next
  cut. It's refused (409) while the cut is being drawn, before the cut has any take, or,
  for a later cut, when its pattern wasn't drawn from the approved keyframe before it.
