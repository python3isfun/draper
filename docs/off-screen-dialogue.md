# Off-screen dialogue lands on the wrong face

Analysis of a clip defect in episode 1 of *Heartbeats of Desire* (shot `sh_01_02_01`,
2026-11-23), what caused it, what was done, and how the pipeline keeps it from happening again.

## The defect

Shot `sh_01_02_01` is an 8 s office scene. McKenna walks in and says "You had sex."; Lila,
not looking up from her prints, answers "I don't even remember sex. Here, draw me a diagram."
and slides a sheet across the desk.

The cut plan put the camera on McKenna's face for Lila's whole line (a 4.8 s close-up, cut 2,
with Lila "off screen left"). Every take H3 rendered of that cut showed **McKenna speaking
Lila's line**, lips in sync, while the prompt said the opposite.

The prompt of cut 2 as written by the video prompt writer:

> ... McKenna, from {pic:img_1ff69f840326} and {pic:img_3f49af2211fe}, occupies center-right
> ... Lila, from {pic:img_ef34e2c27eaa} and {pic:img_2f420890a738}, remains off screen left.
> From 2.4 to 6.5 s Lila says in an off-screen voice: "I don't even remember sex. Here, draw
> me a diagram." Her delivery is breezy and dry. Only Lila's lips move off screen; McKenna
> keeps her mouth closed throughout the line, then lowers her gaze toward the desktop. ...

## Why H3 does this

MiniMax H3 Ref2VA renders picture and sound together. A quoted line is performed, and the
performance is a face: the model lip-syncs the words to whichever face is in frame. It has
no notion of a voice without a body; "off-screen voice" is a weak textual hint against a
strong learned prior that *dialogue is spoken by the visible person*.

Three things in the prompt pushed the wrong way at once:

1. **A quoted line with one face on screen.** The only lips available were McKenna's.
2. **"Only Lila's lips move off screen."** The writer applied its on-screen template
   ("Only X's lips move") to an off-screen speaker. The model reads "lips move" and finds
   lips; it cannot read "off screen" as "not visible", because to it every mentioned thing
   is visible.
3. **Lila bound to her pictures in the same sentence as "remains off screen".** Naming a
   person with their face and look pictures is the writer's rule for introducing someone *on
   screen*. For an absent person it hands the model a second identity to put in the frame.
   The skill's own warning applies: what the prompt mentions, even negated, it tends to add.

The negatives ("keeps her mouth closed", "does not speak") did not help, for the same reason.

## Why the pipeline let it through

The pipeline already knew this plan was risky and said so. The plan check in
`src/drama_agent/shots/plans.py` has a pace rule, `LISTENER`: at a brisk pace a cut may hold
on a listener for at most 1.5 s while someone off screen speaks. It flagged cut 2:

> a listener held 4.8 s in sub-shot 2 while Lila Montgomery speaks off screen; at this pace a
> reaction holds at most 1.5 s: put the speaker on screen for their line

But the rule is **advisory** (`advisory()`): it is shown on the board and in the export and
never refuses a plan. The planner is asked once to fix it; here it answered with a note
justifying the hold ("carrying Lila's uninterrupted reply off screen while preserving both
existing keyframes") and the plan was accepted with the problem attached. Nothing downstream
reads plan problems: the keyframe stage drew and approved the close-up, the prompt writer wrote
the quoted line into it, the clip was rendered twice per take setting, and no clip check
looks at who is talking.

So the failure is a chain of four gaps:

| Stage | What exists | Gap |
| --- | --- | --- |
| Cut plan | `LISTENER` pace rule flags the hold | Advisory; a planner note overrides it; the reason given is pace, not "H3 cannot do this" |
| Prompt writer | "says in an off-screen voice" + "people on screen keep their lips closed" | Still quotes the line; applies the on-screen lips sentence; binds the absent speaker to pictures |
| References | Fast mode sends faces of people on screen in any cut | Harmless here, but the prompt text named the absent face anyway |
| Clip check | None for speech | A take that puts a line on the wrong face is approved like any other |

## What was done for this shot

Option 1: put the speaker on screen for her line. The plan is now three cuts, saved by hand
through the plan endpoint (`POST .../keyframes/sh_01_02_01/plan`), and the check passes
with no problems:

| Cut | Time | View | Speech |
| --- | --- | --- | --- |
| 1 | 0 to 2.4 s | Two-shot, Lila left, McKenna entering right | McKenna, on screen: "You had sex." |
| 2 | 2.4 to 6.5 s | Medium single of Lila, McKenna off right | Lila, on screen, the whole line; she slides the sheet on "Here," |
| 3 | 6.5 to 8 s | Close-up of McKenna, Lila off left | Silent reaction, 1.5 s, the brisk limit |

Line timing comes from the storyboard's dialogue cues (2.4 to 6.5 s for Lila's line) and a cut
may not fall inside a line, so the cut points were fixed at 2.4 and 6.5 s; only the views
changed. The running episode job (two fast takes per shot) picked the new plan up on its next
reconcile: the shot went back to the keyframes stage, the old keyframes and clips were
withdrawn as out of date, and it will redraw, rewrite the prompt and render again.

## Preventing it systematically

The defect is not a wording problem, so the first line of defence is the plan, and the writer
and a check back it up. Each layer below catches what the one before lets through.

### 1. Plan: an off-screen line with a face on screen is a hard problem

Change the `LISTENER` rule from "too long at this pace" to a production rule: **a line spoken
by someone off screen while another person of the shot is on screen is refused** unless the
cut is short enough to be a reaction (the pace limit, 1.5 s brisk, 2.5 s measured, 4 s slow
burn). The message says why in H3's terms: "H3 lip-syncs a quoted line to the face in frame;
show the speaker, or cut to them at the start of the line."

- Refused, not advisory: the planner must fix it (it already gets one retry with the problems
  listed) and a hand-saved plan with it is rejected with a 422, as other spec problems are.
- The planner guide gets the same rule up front, so the first plan is right more often: a
  speaker is on screen for their line; a listening close-up is a short insert *between*
  lines, never *over* one.
- A line from outside the shot (a phone, a narrator, "voice") stays allowed: there is no
  body to show and the writer treats it as sound (see 2).

Keyframe approvals are not affected: the rule runs before anything is drawn.

### 2. Writer: never quote a line nobody on screen can say

For a line whose speaker is off screen (`where` is "off screen <side>" or "voice") while
anyone is on screen, the writer:

- **does not quote the line in the picture text** and does not say "lips move";
- writes the listener's state in the positive: "McKenna listens in silence, mouth closed
  throughout; no one in frame speaks";
- **does not bind the absent speaker to their pictures**; they are named at most as the
  direction of a look ("facing Lila, who is off frame left");
- puts the words in the `sound` sentence as voice-over: `Off screen, Lila's voice, breezy and
  dry: "I don't even remember sex. ..."`, which H3 treats as sound design rather than a
  performance to lip-sync.

This is enforced in two places: the writer guide (`VIDEO_GUIDE` in
`src/drama_agent/shots/video_prompt.py`, rule 6) and the deterministic fallback writer
(`template_prompt`), plus a `video_findings` check that refuses a written prompt whose
picture text quotes a line of an off-screen speaker, so a model that ignores the rule gets a
second try with the problem named.

With layer 1 in place this case only arises for lines that are allowed off screen (short
reaction inserts and voices from outside the shot), where the voice-over form is the right one.

### 3. References: send only who is in frame

The clip's reference pictures are keyframes plus faces. In fast mode faces are already limited
to people on screen in some cut of the shot. The remaining step is to keep an absent person
out of the *text* (layer 2); the picture list needs no change, since a face sent but not
cited is not drawn.

### 4. Check: who is talking in the take

A cheap post-render check closes the loop for anything the rules miss. For each cut whose
plan has an on-screen speaker, sample frames from the clip over the line's time span and ask
the vision model one question: "Which person's mouth is moving?" A take whose answer names
someone other than the planned speaker is marked failed with the finding, so it is not used
in the cut and the Episode tab shows why. The same check reports a silent cut where lips move.
This is an optional run setting at first (it costs one vision call per take) and runs only on
cuts with dialogue.

### 5. Visibility

The board already shows plan problems; the Cuts step of the Episode tab now embeds the plan
editor, so the problem text is visible where the plan is edited. With layer 1 the run holds
the shot on a refused plan instead of rendering through it, the way it holds on a failed
keyframe check.

## Order of work

1. Writer rule and template, with the `video_findings` check (small, local to
   `video_prompt.py`; the change promised for after the current run).
2. Plan rule made strict, planner guide updated, tests for the refusal and the allowed cases.
3. Speaker check on rendered takes, behind a run setting.

Items 1 and 2 are code changes to the live server, so they wait for the current episode run
to finish (the server reloads on every Python edit and a reload kills in-flight renders).
