# Content safety, intimacy and likeness: design notes

Status: discussion record and engineering guidance, November 2026. Sections 2–4 describe what
is implemented; section 9 tracks the proposals and which have since been built.

> This is engineering guidance, not legal advice. Laws on AI images, likeness and intimate
> imagery are changing quickly. Check a company-wide policy on real people's likenesses
> with NewsBreak's legal team.

## 1. Summary

- A story's **rating** follows the MPA film ratings (G, PG, PG-13, R). It caps how intimate
  a scene may be (hard checks enforce it; the hand-off zip includes an **intimacy plan** for
  the intimacy coordinator) and what every image of the story may show (section 3.1). It
  can be changed on the story's Overview.
- **Adults only:** anyone in an implied or simulated scene must have a stated adult age. A
  character who reads as under 18 blocks the job, and the fix is in the script, never in the
  stated age.
- The **content guard** never lets the image model draw, at any rating or mode:
  - sex acts shown explicitly, genitals or pornography (nudity only at R);
  - romantic, sexualized or revealing images of minors;
  - sexualized images of a real person's likeness (from a photo, or a named real person).
- **Real people's likeness:** the Experiment tab tracks whether each image shows a real
  person's likeness. A **Restrictive mode** switch (off by default) decides how much
  exploration with a likeness is allowed (section 3.4); clearing a likeness for use is
  production's and legal's call. The **design board always uses the restrictive rules**,
  because its images go into the hand-off package.
- **Internal testing** gives room to experiment, but some lines hold regardless of
  distribution (section 6).

## 2. Intimacy in the story pipeline

### 2.1 Content rating (job option `content_rating`)

The MPA film ratings. NC-17 is not offered.

| Rating | Highest scene level | Nudity in the script |
| --- | --- | --- |
| `G`, `PG` | kiss or embrace | none |
| `PG-13` (default) | implied: the scene cuts away before anything sexual | none |
| `R` | simulated intimacy on screen | partial at most (bare back, shoulders), on a closed set |

The web UI shows it under **Options** on the new-job form, and on the story's Overview,
where it can be changed (`PUT /v1/drama-packages/{id}/rating`, kept in `story_ratings`
apart from the jobs, which a running pipeline saves from its own copy). The change holds
for the story's images and for revisions of its script; written scenes are not changed. Jobs stored before the MPA ratings read `general`,
`teen` and `mature` as PG, PG-13 and R.

### 2.2 The scene `intimacy` block

A scene with physical intimacy carries the following fields (`models/screenplay.py`); scenes
without intimacy leave the block empty.

| Field | Meaning |
| --- | --- |
| `level` | `kiss`, `implied` or `simulated` |
| `character_ids` | who is in the intimate moment |
| `on_screen` | what the camera shows, written as staging |
| `off_screen` | what is implied but not shown |
| `nudity` | `none` or `partial` |
| `consent_beats` | moments where consent is clear on screen |
| `closed_set` | whether only essential crew may be present |

The script writer is told to write these as directions for the director, never as explicit
description, and never to write romantic or sexual content involving anyone under 18. The
Script tab highlights these scenes.

### 2.3 Hard checks (`validation/hard_checks.py`)

| Check | Fails when | Fixed by |
| --- | --- | --- |
| `intimacy` | the level exceeds the rating; nudity the rating doesn't allow; implied or simulated without consent beats; simulated or nude without a closed set; empty `on_screen`; listed characters not in the scene | the script step (retried with the feedback) |
| `intimacy_age` | someone in an implied or simulated scene has no apparent age in years (e.g. "young") | the cast step: state the age as the story has it |
| `intimacy_minor` | someone in an implied or simulated scene reads as under 18 | the script: remove the intimacy. The job stays blocked until then |

**Why the minor check is separate.** An earlier version told the cast step to "give an age of
18 or older". Under that pressure a model could simply restate a 16-year-old as 18 and pass
the check, while the script still showed a high-schooler. Now the cast step is only ever asked
for an honest age, and a minor blocks the job with the explicit note: *"Do not change the
character's age to pass this check."*

**How ages are read** (`apparent_adult`):
- A stated age decides. "About 40, mother of teenage kids" is an adult.
- Heights, weights and counts are ignored ("5'6\"", "170 cm", "2 kids").
- Words like "teenager" or "high schooler" count only when no age is given.

### 2.4 Hand-off: the intimacy plan

When any scene has intimacy, the zip includes `08_intimacy/intimacy_plan.md`. It is text
only, and lists each scene with its staging alongside the on-set rules:
- an intimacy coordinator (SAG-AFTRA expects one for nudity and simulated sex);
- agreed choreography, modesty garments and nudity riders;
- adult actors only, and consent that can be withdrawn at any time;
- a closed set with restricted monitors and no stills without written consent.

The scene headings in the episode scripts point to the plan.

## 3. Content guard for images (`content_guard.py`)

### 3.1 Rules

1. **By the story's rating** (PG-13 for a picture outside any story, e.g. a scratch chat):

   | Image | G, PG | PG-13 | R |
   | --- | --- | --- | --- |
   | Clothed romance, swimwear | yes | yes | yes |
   | Suggestive: lingerie, undressing, a covered bed scene | no | yes | yes |
   | Nudity of adults, as in an R-rated film (bare breasts or buttocks, a love scene under sheets) | no | no | yes |
   | Sex acts shown explicitly, genitals, pornography | no | no | no |

   Every image check follows it: chats and face studies (each message records the rating
   it was held to; a retry uses the current one), design pictures and keyframes, in both
   layers (the word lists and the model screen, whose rules say the rating).
2. **Never** romantic, sexualized or revealing imagery of anyone who is, or looks, under 18.
3. **Never, at any rating,** a **sexualized** image of a **real person's likeness**: nudity, lingerie or underwear,
   undressing, bed scenes or bedroom intimacy, suggestive poses, degrading situations.
   A real person's likeness is:
   - an identifiable person from an uploaded photo (anyone whose face is visible counts);
   - a picture already marked as showing a real person's likeness;
   - anyone made to look like a named or described real person.

   An upload used only for its clothing, a place or an object passes on nobody's likeness,
   even if a model wears the clothes.
4. Everything else is allowed, including clothed romance (a kiss, an embrace, holding
   hands) and swimwear between adult characters.

### 3.2 Two layers

- **Word lists** (`text_guard`, `upload_guard`) run on **every prompt before any render**,
  in both the design board and Experiment. They are cheap and certain for clear cases.
  - "Nude" as a color ("nude lipstick", "nude-colored heels") and negations ("no nudity")
    are not refused.
  - Design board, for jobs with character-reference photos (or uploads with no stated
    use): underwear, lingerie, swimwear and bed scenes are refused as well.
- **A GPT check** judges what word lists can't: who is in a picture, how old they look, and
  what an edit implies. It sees the pictures.
  - **Rewrite with GPT:** the Experiment prompt writer applies the rules itself and refuses
    instead of writing a toned-down prompt.
  - **Exact prompts and hand-edited design takes:** a separate GPT check (`screen`) runs.
- **Fails closed:** if the check can't run, nothing is drawn.

### 3.3 Likeness tracking

- **Experiment:** each result records whether it shows a real person's likeness
  (`likeness`), and the UI tags it "real-person likeness".
  - GPT judges it when the image is made: kept from a picture that has one, taken from a
    face in an uploaded photo, or a named real person. The answer is required; a missing
    one counts as a failed check, and nothing is drawn.
  - A result inherits the likeness of the picture it changes (`<Picture 1>`). A result made
    from no picture inherits from the pictures GPT was shown, so describing a person instead
    of sending the photo doesn't drop the likeness.
  - Going back to an image without a likeness ("go back to #3") starts clean.
  - Images made before tracking existed are judged by GPT from the picture and its full
    ancestry (every message and prompt it was built from).
- **Design board:** the uploaded-photo rule applies when a job behind the package has
  uploads used as character references, or with no stated use. Location and style
  references don't count.

### 3.4 Restrictive mode (Experiment)

A switch at the top of the page, **off by default**, remembered per browser. Rules 1–4
apply in both modes.

| | Off: exploration (default) | On: restrictive |
| --- | --- | --- |
| Resemble a named real person | Allowed | Not allowed: GPT describes generic traits and keeps names out of prompts |
| Take a face from an uploaded photo | Allowed | Not allowed |
| Clothed romance or swimwear with a likeness | Allowed | Not allowed |
| Sexualized image of a likeness | Never | Never |

The **design board always uses the restrictive rules**, whatever the switch says. Its
edited takes are screened under them, and its prompt writer never makes a character
resemble a named real person.

### 3.5 What the user sees

- **Experiment:** a refused message shows a red "not allowed" with a one-sentence reason.
  **Check again** runs the check under the current rules, which helps after a rule changes.
- **Design board:** a refused edited take is rejected immediately. An image the worker
  refuses fails with "Not drawn: …".

## 4. Experiment tab: reference images

When a chat has a current image and the message has attachments, all of them go to H3 in one
edit request:
- `<Picture 1>` is the current image, the one being changed.
- `<Picture 2>`, `<Picture 3>`, … are the attachments, in the order they were attached.
- An upload sent **without text** replaces the current image instead.

**Rewrite with GPT** names each picture as a subject, writes a one-line summary of the
target, and gives each picture exactly one role. The current image is usually kept except for
the change, and an attachment supplies one thing (a jacket, a background, a light) or acts as
a loose style reference.
- **Tip:** say what to take, e.g. "put her in the jacket from the photo". "Use this" leaves
  the model guessing.

**Exact prompt** sends your text as written, so you write the subject definitions yourself.
Citing a picture that wasn't sent is refused.

**Limits:**
- **Pictures:** at most 9, the current image included.
- **Size:** together they must fit 24 MiB. If they don't, uploads are sent as their
  1,536 px copies.
- **Output shape:** with "match the current image", the output follows Picture 1's shape.

**Guard effect:** only an upload that passes on a person's likeness (a visible face whose
likeness the result keeps) marks the result as showing a real person's likeness. A garment,
place or object reference doesn't, even if a model wears the clothes (section 3.3).

## 5. Real people's likeness: where the law stands

| Area | Status |
| --- | --- |
| Sexual imagery of minors | **Illegal**, generated or not |
| Intimate deepfakes of real people | Publishing them is a **federal crime** (TAKE IT DOWN Act, 2025); many states also ban making or sharing them |
| Commercial use of a likeness without consent | **Actionable** under right-of-publicity laws in many states; several states now address AI digital replicas directly |
| Internal lookalikes, no distribution, no commercial use | **Gray:** publicity rights usually turn on use, so private experimentation is weakly covered, but it's untested and replica laws are expanding |
| Where "inspired by" ends and "replica" begins | **Gray:** describing traits is safe; a face recognizable as a specific person is the risk zone |
| Provider terms (OpenAI, MiniMax via MCPHub, company policy) | Contractual, not criminal, but they often bar impersonating real people even internally. **The most likely source of a real problem** (e.g. a suspended account) |

**In practice:** internal exploration is low risk while nothing intimate is involved, nothing
leaves the company, and nothing ends up in the production package. The risk is **drift**: a
test image becoming the casting reference the team shoots from.

## 6. Internal testing: what's fine and what isn't

**Fine:**
- **Testing the guard:** prompts that *should* be refused. Refused prompts never reach H3,
  so nothing gets made.
- **Creative tests within the rules:** clothed romance between fictional adults, ratings,
  intimacy plans, casting looks.
- **Real faces with consent:** a colleague who agreed, an actor's sign-off for casting
  tests, licensed stock photos.

**Not fine, even internally and undistributed:**
1. Sexualized images of anyone who is, or looks, under 18.
2. Intimate, nude or revealing images of a real, identifiable person without consent.
3. Anything the providers' usage policies or company policy forbid. Explicit nudity is
   generally prohibited regardless of purpose.

**Engineering rule:** keep the guard always on, and **don't add a "test mode" bypass**. A
bypass switch is what ends up enabled by mistake. If a rule blocks a legitimate test, narrow
the rule, as with the "nude lipstick" fix.

## 7. Likeness requests: cases and current behavior

### 7.1 "Make the woman look like this actress" (with her photo attached)

- **What happens:** the photo becomes `<Picture 2>` and GPT writes a face transfer. The guard
  allows it, since copying a face isn't an intimate edit. The result is marked as showing a
  real person's likeness. With Restrictive mode off, clothed romance with it is allowed and
  sexualized edits are refused; with it on, the face transfer itself is refused.
- **Risk:** the result is a recognizable replica of a real person.

### 7.2 "Make her look like [actress name]" (text only)

> **Update:** this is now handled (section 3.3). A named real person is tracked as a
> likeness, just like a photo. With Restrictive mode off, the likeness may appear in
> ordinary and clothed romantic scenes; with it on, GPT describes generic traits instead.
> Sexualized images of the likeness are refused in both modes.

Original analysis:

- **What happens:** GPT will likely keep the name in the H3 prompt, and H3 may reproduce a
  famous face.
- **Gap:** the guard treats the result as text-only, so later "now they kiss" or "put her in
  bed" get the looser rule meant for fictional characters. **A named real person bypasses
  rule 3.** Proposal 9.2 closes this.

### 7.3 Composite features: "Alice's hair, Bethy's lips, Cindy's chin"

- **Risk:** much lower than copying one person. Likeness law turns on whether a specific
  person can be identified, and a mix of generic features usually produces a new face.
- **Still watch for:**
  - a **signature feature**, such as an iconic hairstyle or famous smile, that dominates the
    result;
  - **one dominant source** ("Alice's face with Cindy's chin" is still Alice).
- **Behavior today:** with photos, each becomes a reference supplying one feature, which H3
  handles well. With real names, the likeness is
  tracked as in 7.2.
- **Private people's photos** (colleagues, friends): get their OK first. If the names are
  your own fictional characters, none of this applies.

### 7.4 Text descriptions without names

- **Mostly fine:** describing visible traits is the safe default and standard
  character-design practice.
- **The test is identifiability, not the name.** Watch for:
  - a stack of traits that can only mean one real person (a signature haircut, a known
    scar, birthmark or tattoo, a trademark outfit and setting);
  - context that identifies them anyway ("the singer who won last year's Grammy for …").
- A description built to land on one specific real person is a replica with the name
  removed.

## 8. Wardrobe from retail sites (e.g. a parka from Macy's)

**Normal costume practice; low risk internally.**

| Concern | Guidance |
| --- | --- |
| The garment's design | US copyright protects clothing very little (a cut or shape is a "useful article"); only separable artwork, such as a print, is protected. Dressing a character in "that parka" is fine |
| Logos and trademarks | Fine internally. A visible brand on screen needs clearance from the production's legal team (often required for E&O insurance) or a product-placement deal. Ask the model to leave the logo off references |
| The product photo | Copyrighted by the retailer. Internal reference is low risk; don't redistribute it or put it in the hand-off package |
| Site terms | Saving an image by hand is fine; automated scraping is a different matter |
| A real model in the photo | Counts as a real person: crop to the garment or use a flat-lay or ghost-mannequin shot. Otherwise the result is marked as from an upload, and H3 may pick up the model's face or body |

**In the app:**
- Your character is `<Picture 1>` and the garment photo is `<Picture 2>`.
- A message like "dress her in the parka from the attached photo, keep everything else, no
  logo" gives a garment transfer.
- Small details (zippers, stitching) may drift, so use 3 draws and pick one.

**For production:** the costume team buys or rents the real item. A product link next to the
reference image is the most useful note for them.

## 9. Proposals

Status: 1 and 2 are built, as Restrictive mode (section 3.4) and likeness tracking
(section 3.3). 3–5 are still open.

1. **Describe a type, not a person.** The Experiment writer turns "like [real person]" into
   generic visible traits and never puts a real person's name into an H3 prompt. It also
   avoids rebuilding a recognizable real person from a stack of distinctive traits, and says
   in its reply that it did this.
2. **Named real people count as uploads.** A message or prompt that names a real person gets
   rule 3, closing the gap in 7.2.
3. **Consent tick for face transfer.** Copying a face from an uploaded photo onto another
   figure requires ticking "I have this person's consent". Casting tests with an actor who
   agreed still work, and celebrity replicas are stopped by default.
4. **Guard robustness tests.** A suite of attack prompts (rephrasings, requests split across
   messages, references used to slip a request in) runs against the offline test models, so
   nothing is sent to H3 or GPT.
5. **Policy review.** Confirm a written policy on real people's likenesses with NewsBreak's
   legal team, covering company accounts and provider terms.

## 10. Where things live

| What | Where |
| --- | --- |
| Intimacy model, rating limits | `src/drama_agent/models/screenplay.py`, `models/request.py` |
| Intimacy hard checks, age reading | `src/drama_agent/validation/hard_checks.py` |
| Script and cast instructions | `src/drama_agent/pipeline/steps.py` |
| Intimacy plan export | `src/drama_agent/export.py` (`intimacy_md`) |
| Content guard | `src/drama_agent/content_guard.py` |
| Experiment writer rules, lineage | `src/drama_agent/experiment.py` |
| Design-board guard | `images.py` (worker, `package_has_uploads`), `api/app.py` (edited takes), `image_prompts.py` (writer) |
| Tests | `tests/test_intimacy.py`, `tests/test_experiment.py` |
| Copyrighted story sources | `stories/`: gitignored, never committed |
