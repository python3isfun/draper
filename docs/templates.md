# Story templates: learn from great films and dramas

Status: built (25 Nov 2026), phase 1.

## Why

Creators start from a one-line idea, and the pipeline finds the story's shape on its own.
Great films and dramas already know what works: how the first seconds grab you, where the
reversals fall, how the camera stages a reveal, how fast it moves. A template carries that
craft from one title into a new story, without its content.

## The line a template keeps

Structure, genre conventions, tropes and technique belong to no one. A title's own
expression does: its characters and their names, its places, its dialogue, its signature
scenes and their order. A template holds the first and never the second:

- **Patterns, not specifics.** "The guarded heir", not a character's name; "a public
  humiliation the lead endures in silence", not a scene from the film; the register and
  rhythm of the dialogue, never its lines.
- **`source_specifics`.** The model lists the title's own names, catchphrases and signature
  scenes while learning. The template's pattern may not contain them (checked when it is
  learned), and neither may a story made from it (checked in the outline and script steps;
  the step repairs and retries, as with any failed check).
- **A script learned from is read, never stored.** Scripts and synopses stay in `stories/`
  (gitignored). A learned template may not copy eight words in a row from the script.
- **Creators see the pattern's name** ("Secret identity, public fall"); the title it was
  learned from is noted on the template card for the team.

One template is learned per title (a pattern from several works would be safer still, but
less close to one work's feel), so the originality checks are strict.

## What a template holds

`templates/<template_id>.json` (`StoryTemplate` in `src/drama_agent/templates.py`):

| part | fields |
| --- | --- |
| card | `name`, `summary`, `genre`, `audience`, `emotional_goal`, `learned_from` |
| story | `premise_pattern`, `hook`, `roles` (role, function, arc), `relationships`, `structure` (beats of episodes 1 to 3, with where in the 120 s they fall), `cliffhangers`, `tropes`, `reversals`, `dialogue_style` |
| design | `look` (medium, color and light, camera and framing, wardrobe and design), `set_archetypes`, `prop_motifs` |
| pace and sound | `pace` (brisk, measured, slow_burn), `pacing_notes`, `music` (a caption for music beds) |
| guard | `source_specifics` |

## Learning one

```
python -m drama_agent.templates learn "<title>" --year 1997 --kind film \
    [--script stories/<dir>/script.txt] [--notes "what to pay attention to"] [--id <id>]
python -m drama_agent.templates list
```

It uses the model from `.env` (`DRAMA_MODEL_CLIENT`), learns from its knowledge of the
title and, given `--script`, from the script too, and writes `templates/<id>.json`. If the
result still names its source's specifics or copies the script after three tries, nothing
is saved. Read the template before committing it; edit it by hand if a beat is too close
to the original.

## Using one

- **New job form:** Template, under the idea. The card shows its hook, shape, roles, look
  and pace. Choosing one sets the pace and fills genre and audience when they are empty.
  Adaptation mode turns it off: adapting keeps the source's own shape.
- **API:** `options.template_id` on `POST /v1/drama-jobs` (422 when unknown, or with
  adaptation mode). `GET /v1/templates` lists them; `GET /v1/templates/{id}` returns one.
- **In the pipeline:** each step's instructions get the template's part, after the step's
  own directive (`template_rules`): every step gets the originality rule; the outline gets
  the whole story pattern; the script the hook, dialogue style and endings; the cast, set
  and prop sheets the look and the kinds of places and objects; the storyboard the look,
  the camera and the pacing (it writes the style brief that the image and keyframe prompts
  read); the review checks the story doesn't retell the source. Revise runs get it too.
- **Provenance:** `prompt_versions` records `template_rules` and `template:<id>` with the
  template's version.

## Next

- A music bed default from the template's `music` caption in episode runs.
- Learning from the UI (a Templates page: title, optional script upload, review, save).
- Templates that combine several titles (a genre pattern).
