# Global image numbers (@1234)

Every picture in the app has a global number, shown at its top right as `@1234`. Write it
in any chat message to bring that picture in, wherever it lives: another chat or face study,
a story's design pictures (candidates and picks), keyframe draws, or uploads.

```
put the jacket from @1234 on her
like @88, but at night
```

`#3` still means image 3 of the chat you're in; `@1234` means the same picture everywhere.
In a face study, a number picks the photo to describe (below).

## What you see

- **The tag** (`@1234`) sits at the top right of each picture: chat and face-study images,
  design draws, library pictures, keyframe draws, a sub-shot's reference pictures, cast
  choices and uploaded sources. Click it to put it in the open chat's message box (the
  workbench panel or the full chat page), or, with a face study open, in its "Describe a
  picture by number" box; otherwise it's copied to the clipboard.
- **Bringing a picture in.** The first time a message mentions `@1234` and that picture is
  not in the chat, the chat gets a "Brought in @1234: ..." step before the message: a copy of
  the picture with its own chat number (`#5`), the prompt that made it, and its likeness.
  It does not become the current image; the message decides how it's used, as with `#5`.
  Mentioning it again reuses that copy. A picture already in the chat (made there, uploaded
  there, the pick it started from, or brought in before) is used as it is.
- A copy shows its original's number, so one picture carries one number everywhere.
- An unknown or deleted number is refused (422) and nothing is added to the chat.

## How it works

- **Refs** (`image_ids.py`): `asset:<asset_id>`, `turn:<turn_id>:<n>`,
  `design:<image_id>`, `take:<take_id>:<n>`. The store's `image_ids` table maps each ref to
  its number; a number is handed out the first time the picture is shown and never changes.
- **API:** `POST /v1/image-ids {refs}` gives (and hands out) numbers;
  `GET /v1/images/{n}` says what the picture is (`ref`, `what`, `likeness`), with
  `/file` and `/thumb` for its pictures.
- **Chat messages:** `POST /v1/experiments/{id}/turns` resolves each `@n` before planning;
  pictures from outside the chat become `import` turns (`ExpTurn.import_ref`), saved only if
  the message is accepted. The message keeps its text as written and records
  `global_mentions` (`"1234"` → its chat number); everything after the API (picture order,
  exact prompts, the prompt writer, the fallback) reads it with `@1234` as `#5`
  (`experiment.chat_text`).
- **Content rules:** a brought-in picture keeps the likeness it was made with (a chat
  result's, or a design or keyframe image's `likeness`), so the floor under the writer's
  judgment (`inherited_likeness`) still holds. As with a pick start, an unmarked story
  picture from a story with uploaded photos (`package_has_uploads`) comes in as unknown and
  is judged from the picture, as are uploads and results from before likeness was tracked.
- **Shape:** a brought-in picture (and a pick start) carries its picture's shape as its
  size, so an automatic size drawn from it keeps a 3:4 face 3:4. Pick starts saved without
  one get it from their slot's picture on the chat's next message. A WebP or HEIC upload
  comes in as its JPEG copy, since chat images go to the image model as they are.
- **Face studies** describe a photo, so there a number picks the photo: **Describe a
  picture by number** in the study's photo card (or `POST /v1/experiments/{id}/describe`
  with `image`). An upload is used as it is; any other picture becomes an upload-style copy
  (`SourceAsset.from_image`, reused when the study describes it again) that shows the
  picture's number. A picture marked as a likeness keeps it: `from_likeness` holds every
  description of the copy to "real-person likeness" whatever the model judges. Tests draw
  from the prompt alone, so a test prompt with `@n` is refused with a pointer to describing
  it.
- Design redraw notes and keyframe directions don't take `@n` yet.
