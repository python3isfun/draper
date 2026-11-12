# MiniMax-H3-Image prompt rules

Shared by the `minimax-h3-image-prompt` skill (`.claude/skills/minimax-h3-image-prompt/`) and the
app's image prompt writer (`drama_agent/image_prompts.py`), which sends this file verbatim to the
text model. Change the rules here, in one place. Sources: the skill's `references/sources.md`.

## Writing rules

### Describe the image

- **Task before style.** Open with what the image is and what it is for ("A studio casting
  photograph of ...", "A production design photograph of the empty set of ..."), then the main
  subjects and their state or action. Style comes after.
- **Layout before details.** Give the number of subjects, their positions (screen-left/right,
  foreground/background), the viewpoint and the empty space. For dense content, name the regions
  first, then fill each.
- **Style through visible details.** Color, material, light (with its source and direction) and
  texture, not "premium", "stunning", "cinematic", "8k" or "masterpiece". Words like "cinematic" or
  "portrait" do not set the canvas shape.
- **Describe light by its effect, not the equipment.** "Soft warm light from the front left" rather
  than "a large softbox at the front left": a named softbox, light stand or reflector tends to
  appear in the frame. Name equipment only when it should be visible.
- **One frozen moment.** For bodies, fix the scene layout and camera first, then state the body's
  orientation to the camera (front-facing, left-facing profile, rear three-quarter, ...) and keep
  gaze, limbs, support and contact consistent with it. State which hand touches what.
- **Exact counts.** Preserve requested counts and keep the number of primary figures to what is
  asked; say "exactly three people: ... on the left, ... in the middle, ... on the right". A
  vague group invites extra people. Background people read as small silhouettes.
- **Visible evidence only.** Use world knowledge as things the viewer can see (materials, tools,
  clothing, architecture), not backstory.
- Write in **English**. Text that appears in the image keeps its own language.

### Text in the image

- **Quote every word that appears**, exactly as given, with its surface, position, size hierarchy
  and line breaks: `a sign on the door reads "OPEN"`.
- **Say when there is no text.** In a text-to-image prompt end with "The image contains no text,
  letters or numbers." In an edit, say that no other lettering is added. (Without this the model
  adds titles, labels and decorative lettering.)
- Charts and data: exact values, units, axes, time ranges; mark example data as illustrative.

### Reference images and editing

Edit prompts are **one paragraph** in this order: source definitions, then the target and each
source's relationship, then the changed details. Narrate the finished image ("the cup is blue"),
not editing commands ("make the cup blue").

1. **Define sources** with stable tokens: `The red cup on the left in <Picture 1> is <Subject 1>.`
   One subject can cite several pictures: `... in <Picture 2> and <Picture 3> is <Subject 2>.`
   Define only scopes that need their own relationship (a person, object, background, text block,
   or a property such as a color).
2. **Target overview**, starting with `The target image ...`: the principal result.
3. **One relationship per subject:**
   - `<Subject N> is fully preserved.` (standalone sentence; covers count, position, state,
     appearance; moving, relighting or occluding it is a change)
   - partially preserved: name what stays and what changes
   - attribute transfer: `<Subject 2> supplies the exact body color transferred to <Subject 1>.`
   - weak reference: `<Subject 2> serves as a weak style reference for ..., with freely adapted forms.`
4. **Changed details:** describe only what changes or is new, concretely (shape, material,
   contact, light). Removal is described as the surface left behind. After full preservation, use
   only the bare token; do not re-describe it.

### Grids and multi-view

- Grids as rows and columns: "2 rows and 3 columns", "row 2, column 1", never "2x3".
- Paired views (plan and render, before and after, front and back) share one inventory and
  coordinate frame: same walls, openings, furniture, labels and relative positions.

## Reference sheets for the drama agent

The app makes these images per asset requirement (`src/drama_agent/images.py`); the text model
turns each image's brief into a prompt (`src/drama_agent/image_prompts.py`).

| Image | Task | Size | Prompt shape |
| --- | --- | --- | --- |
| Character identity (front face) | text-to-image | `3552x4736` | casting photograph; head and shoulders; front-facing; plain backdrop; light source; identifying features; no-text sentence |
| Look (full body) | edit, `<Picture 1>` = face | `3072x5472` | define the person in `<Picture 1>` as `<Subject 1>`, partially preserved (face, age, build, identifying marks stay; wardrobe, hair, condition, pose change); full-length, head to shoes |
| Set, first state (wide) | text-to-image | `4736x3552` | production design photograph of the empty set; eye-level wide; layout, materials, palette, entrances; light source |
| Set, other state (wide) | edit, `<Picture 1>` = first state | `4736x3552` | the set is `<Subject 1>`, partially preserved: layout, furniture, materials and camera stay; time of day, light, dressing, condition change |
| Set plan (top-down) | edit, `<Picture 1>` = wide | `4096x4096` | same inventory and coordinate frame as the wide view; overhead plan-view; every opening and piece of furniture |
| Prop, first state (detail) | text-to-image | `4096x4096` | product photograph; close-up; materials, wear, distinguishing marks; for a picture inside the prop, who is in it, exact count and positions |
| Prop, other state | edit, `<Picture 1>` = first state | `4096x4096` | the prop is `<Subject 1>`, partially preserved; only the state changes |

Must-keep items from the design sheets must be **visible** in the image (e.g. the scar on the
back of the right hand: frame so the hand shows). Facts come from the cast/set/prop sheets; turn them into concrete visual English and keep
character and place names as written.

## Final check

- Every word that appears is quoted and exact; the no-text sentence is present when no text is
  wanted.
- Counts, left/right, foreground/background, orientation and contact match the request.
- Each reference picture has a `<Subject N>` definition and exactly one relationship; nothing cites
  a picture that was not supplied.
- What is preserved is named; edits narrate the finished image.
- The size matches the deliverable's aspect ratio at about 16 MP.
- No vague quality word remains where a visible detail can say it.
