# Characters and Sets in layers

A character or set page opens in three layers, each a link with a breadcrumb back:

```
Characters › Lila Montgomery › Look: Evening torn › full body
#lib=chr_…     (the character)
#lib=chr_…&part=look_…             (one look, state or item; the character itself for its face)
#lib=chr_…&part=look_…&slot=d_…    (one picture: its takes)
```

1. **The character or set:** its line, its sheet (folded; Edit in place), where it's used,
   and its looks or states and items as cards. A card shows the picture's pick, the look's
   line, its number of takes and of shots (or "in no shot yet"), and "out of date". A
   character's first card is its **Face**. **+ New look** (or **+ New state**) is here.
   Other stories' pictures of a linked character stay under "Also drawn in".
2. **A look, state or item:** its line and sheet (Edit), the shots that use it, and a card
   per picture (a look's full body; a state's wide and top-down views), each with its pick
   and number of takes.
3. **A picture:** the New takes controls (draws, draft or full size), the workbench row
   (Develop in a chat, From a photo, Create from text, its chats), its **Takes**, what was
   **Brought in**, and **New take** (or what must be picked first).

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
