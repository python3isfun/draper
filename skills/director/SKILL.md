---
name: director
description: Review and direct shot selection and pace for Draper's vertical short dramas. Use when reviewing a rendered episode or a reference clip (an .mp4), comparing our output with a mature drama, critiquing or planning a storyboard's coverage (shot sizes, reactions, rhythm), deciding why an episode feels slow or stagey, or changing the pace, coverage, script or style-brief rules in src/drama_agent (pace.py, validation/hard_checks.py, pipeline/steps.py). Measures cuts and loudness with scripts/measure.py and writes findings to director/.
---

# Director: shot selection and pace

A vertical drama lives or dies on which shot is on screen and for how long. Our pipeline
draws good frames; what makes an episode feel amateur is the plan behind them: long
two-shots, no reactions, lines too long to cut on. This skill is the director's eye. It
measures a clip, reads every shot, compares it with the benchmark, traces each gap back to
the Draper step that caused it, and writes the result to `director/`.

The founding study is
[director/2026-11-25-ep4-vs-heartbeats-ep1.md](../../director/2026-11-25-ep4-vs-heartbeats-ep1.md):
a mature third-party episode preview against Heartbeats of Desire, episode 1. Read it once:
every rule below comes from it.

## The craft (what mature vertical drama does)

1. **Cover dialogue in close singles.** In a 9:16 frame, medium close-up is the default
   size. Shot/reverse shot between speakers, over-the-shoulder to keep the other person
   present. A full-figure two-shot leaves each face about a tenth of the frame: use it only
   for an entrance, a power picture (allies shoulder to shoulder, a standoff), or a move to
   a new beat. Every wide frame must do something a close-up can't.
2. **Cut on the line and on the reaction.** The benchmark's median shot is 1.6 s; a cut
   lands on nearly every line, often a second one inside the line, onto the listener. Speed
   comes from cutting, not camera moves. Allow **one** long hold per scene (up to about
   5 s) for its emotional turn: one hold in a fast clip makes that moment heavier.
3. **The listener's face carries the turn.** Play the key line on the person it lands on:
   the threat on the rivals' faces, the tender question on her eyes. Use 0.5 to 1 s
   micro-reactions (a half-smile, a glance down) before a one-word turn.
4. **Separate people and worlds with light, depth and wardrobe.**
   - Light: motivated, with contrast, and shallow focus on singles.
   - Palette: one per world.
   - Signature: one per lead (a garment, a color) that contrasts with whoever opposes them.
   - Distinct faces for everyone. Look-alikes are fatal at 2 s a shot.
5. **Write for short shots.** Lines average about 5 words, and the turns are 1 to 3
   ("'Temp'?", "Are we clear?", "I said now."). Open inside a conflict already under way;
   end on a line that reverses or raises the stakes. Prefer one location per beat and
   change the beat by who enters or leaves.

Guardrails: keep faces and backgrounds consistent from shot to shot. The benchmark's own
flaws are a background that jumps mid-exchange, face drift, look-alike secondary cast, a
sideways end card and mid-frame subtitles. Learn the pattern of a reference clip, never
its characters, lines or scenes (see `docs/templates.md`).

## Targets (brisk pace)

| Metric | Benchmark clip | Target |
| --- | --- | --- |
| Median shot | 1.6 s | 2.0 s or less |
| Shots over 4 s | 3% | 10% or less |
| CU + MCU + over-the-shoulder | 69% | 60% or more |
| Wide two-shots and group shots | 28% | 30% or less |
| Words per line, average | about 5 | 6 or less |
| Lines over 8 words | about 10% | 15% or less |
| Reaction shot at each turning line | yes | yes |
| Audio peak | -1.6 dB | below -1 dB |

Measured and slow-burn stories may hold longer, but the coverage rules (1, 3) still apply.

The shot targets are sub-shot targets. Since 2026-11-26 a storyboard shot is one clip of
4 to 8 s (H3 renders nothing shorter than 4 s) with every line inside one shot
(`pace.COVERAGE_RULES`, checked by `check_coverage`), and the cut planner is told to change
the view every 1.5 to 3 s at a brisk pace (`setup_s` in `pace.RULES`). The listener cap
(`reaction_s`) and the view length are advice to the planner, not checks. A storyboard
written before that date has shots of 3 to 6 s and lines that run over cuts: say which
rules a revision was written under before scoring it
([references/draper-levers.md](references/draper-levers.md)).

## Workflow: review a clip or an episode

1. **Measure.**
   `python skills/director/scripts/measure.py <video> [<video> ...]` prints duration, size,
   shot count, mean and median shot, shots over 4 s, cuts per minute and loudness. It
   writes `shots.json` and a contact sheet `shots.jpg` (one frame per shot, labeled
   `<n> <start>s +<length>`) to the temp folder's `director-measure/`. Our episode renders
   are `data/media/episodes/<package_id>/<run_id>.mp4`.
2. **Fix the cut list by eye.** Scene detection fires on light changes inside one shot (a
   walk past a window) and misses cuts between similar frames. Look at the sheet. Re-run
   with `--merge t1,t2` to drop false cuts, or `--threshold 0.2` to catch soft ones.
3. **Read every shot** from the sheet (open frames in `frames/` when a thumbnail is
   unclear):
   - size: CU, MCU, MS, OTS, full figure, wide, group, insert;
   - subject;
   - the line on screen and whose it is (the speaker, or a listener while someone off
     screen speaks);
   - what the shot does (entrance, challenge, reaction, power picture, transition, hold,
     button).

   Build the shot table, then count the shares.
4. **Listen and check faces** (our episodes only). Neither shows in a contact sheet of
   one frame a shot.
   - **Speech:** transcribe the episode
     (`ffmpeg -i <video> -vn -ac 1 -ar 16000 ep.wav`, then
     `uv run --no-project --with openai-whisper --with "numpy<2.3" whisper ep.wav --model small --language en --output_format json`)
     and read it against the script's lines in order. Look for a line heard twice, words
     the script doesn't have, and a line that stops short. The speech check in the app
     only looks at whose lips move, so it passes all of these. A transcript mishears rare
     words; confirm a finding by ear before reporting it.
   - **Faces:** tile each lead's approved keyframes beside their face picture
     (`montage`), then a mid-frame of the matching clips. The clip copies its keyframe, so
     a face that changes is almost always a keyframe to redraw, not a render to repeat.
   - **Continuity:** read two frames a second (`ffmpeg -vf fps=2,scale=256:448`), not one
     a shot, for props that reset and hands that belong to nobody.
5. **Trace our gaps upstream** (our episodes only). The render can't be closer or faster
   than its plan:
   - **Storyboard:** each shot's `framing`, `camera`, `duration_ms` and line count, from
     `GET /v1/drama-packages/<id>` (`shot_list`). Check the revision against
     `git log -S"COVERAGE_RULES"`: was it written under the current rules?
   - **Lines:** the words per line, from `episodes[].scenes[].beats[].dialogue.text`.
   - **Cut plan:** the episode run's settings, from the `episode_runs` table.

   Snippets are in [references/draper-levers.md](references/draper-levers.md).
6. **Score against the targets,** and list the reference's flaws so nobody copies them.
7. **Write the report** to `director/<YYYY-MM-DD>-<slug>.md` with
   [references/report-template.md](references/report-template.md). Contact sheets stay
   in the measure folder, ours included: the report cites shot numbers and start times, and
   gives the measure command that rebuilds the sheet. **Never save a third-party clip's
   frames in the repo.** (The founding study's sheet predates this rule.)
8. **Recommend changes in Draper,** ranked by impact, each naming the file and rule
   ([references/draper-levers.md](references/draper-levers.md)). Re-measuring after a fix
   is part of the recommendation.

## Workflow: close the gap to a reference, round by round

Use this when the job is "make ours as good as that clip". One round changes one thing,
so the next measurement says whether it worked.

1. **Score both.** Measure the reference and our newest cut with the same command, and
   keep the scorecard in `director/<date>-<slug>-scorecard.md`: median shot, shots over
   4 s, cuts a minute, the share of close singles, words per line, lines that differ from
   the script, leads whose face changes, continuity breaks, audio peak. Add a row a round.
2. **Take the largest gap that is ours to close,** furthest upstream first: script, then
   storyboard, cut plan, design pictures, keyframes, video prompt, render, assembly. A
   round that fixes keyframes under a storyboard that will be rewritten is thrown away.
3. **Write the rule before the fix.** Put what the round learned into this skill (a craft
   rule, a checklist line, a row in the levers table) and into the app where the row
   points, with a test. Then apply it to the episode. A fix made by hand in one episode
   and not written down comes back in the next.
4. **Regenerate only what the change touches,** and look at stills before video: a
   keyframe sheet costs minutes, a render an hour. Render one take while iterating; two
   only for a cut that will be shown.
5. **Review the new cut with the review workflow above,** fill in the scorecard, and stop
   when a round moves nothing or the remaining gaps are the model's (resolution, motion
   quality), not the plan's.

Never copy the reference's lines, characters or scenes: the scorecard compares craft.

## Workflow: critique or plan a storyboard (before rendering)

Read each scene's shots in order and check:

- **Opening:** does the scene start inside the conflict, with one placing shot at most?
- **Dialogue coverage:** are lines covered by alternating singles or over-the-shoulder
  shots, rather than one two-shot carrying several lines?
- **Reactions:** does every turning line have a close-up of the person it lands on?
- **Length:** at most one line per shot at a brisk pace; 1.5 to 3 s per shot; one hold
  for the turn, with its reason in `camera`.
- **Wides:** is each wide or group frame doing a job (entrance, power picture,
  transition)?
- **Props:** is each prop picked up once and put down once in the scene? A prop planted
  and retrieved again reads as a loop once the shots are cut together.
- **Lines and shots:** does every line start and end in a shot its speaker is in? A part
  cued over someone else's single is moved to the speaker's shot and heard over the
  listener, which works for a few words and not for a whole sentence.
- **Variety:** no two consecutive shots share the same size and the same people
  (`check_coverage` enforces this).
- **Ending:** does the scene end on a button: a line, a look, an insert, a reversal?

Cheap rule of thumb: count the shots. A brisk 30 s scene needs about 12 to 15, not 4.

## Gotchas

- **Draft renders** are 512x896, so judge softness and look at full size, but pace and
  coverage at any size.
- **A clip with no cuts for 6 s or more** is usually a storyboard shot the cut planner
  couldn't split, not a camera choice. Look at the plan.
- **Scene detection and peaks:** detection reports pts times. A peak at 0.0 dB means the
  mix hits full scale. The assembly limits the mix to about -1 dB
  (`shots/assemble.py`, `PEAK`); a hotter peak is a clip from before that, or the encoder.
- **MCPHub credits:** when they run out (HTTP 402), prompts stop being written and
  checks stop running. Look at a failed prompt's error before blaming the rules.
- **The run's time estimate** averages old, slow renders: a clip takes about 80 to 100 s.
- **Editing any `.py` file reloads the local server** and pauses a run or drops a draw in
  flight. Edit between runs.
- **This ffmpeg has no `drawtext`:** label frames with ImageMagick (`montage -label`), as
  measure.py does.
