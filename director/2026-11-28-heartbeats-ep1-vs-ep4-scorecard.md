# Heartbeats of Desire, episode 1, against the reference clip: scorecard

Started 28 Nov 2026. One row a round, as the director skill's "close the gap" workflow
asks. The reference is `~/Downloads/ep4_preview.mp4` (a third-party vertical drama,
episode 4 preview; see [the founding study](2026-11-25-ep4-vs-heartbeats-ep1.md)). Only its
craft is compared, never its lines, people or scenes.

## Scores

Measured with `skills/director/scripts/measure.py`, a `whisper` transcript read against
the script, and frames at one or two a second. Faces and continuity are judged by eye.

| Cut | Median shot | Shots over 4 s | Cuts a minute | Lines that differ from the script | Lila's face | Continuity breaks seen | Audio peak |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Reference | 1.5 s | 3% | 32 | not scored | not scored | not scored | -1.6 dB |
| `run_577cedaa0d8b` (27 Nov, first render) | 4.1 s | 55% | 16 | 6: one line twice, two passages of nonsense, three small | changes in most shots | shoe picked up four times; stray hand; card changes | -0.3 dB |
| `run_64903545a792` (round 0: speech, bedroom, face prompt) | 3.9 s | 45% | 17 | 1: extra words after "Sorry about that" | steady in the bedroom, changes in the garage | stray hand; a half-second insert | -0.7 dB |
| `run_fa0f8c73e6ce` (round 1: new work-outfit picture, garage keyframes) | 3.9 s | 34% | 17 | 1: the same | steady in the bedroom and the garage; rounder in the office | Ryder's hair slick in three shots, curly in the rest; Lila turns to camera with another face and bag at 127 s; the card is a sheet of paper | 0.0 dB |

Not yet scored against the reference: the share of close singles (76% in the storyboard,
69% in the reference) and words per line (7.0 against about 5).

## Rounds

### Round 0: speech, the bedroom, the face prompt (27 to 28 Nov, commit `0fbdc14`)

- **Found:** lines doubled or garbled where a part of a line was cued over a shot its
  speaker isn't in; the storyboard had Lila pick the shoe up twice; the keyframe prompt
  kept the face of the outfit picture.
- **Changed:** a part goes to its speaker's own shot and is laid over the listener at
  assembly; two bedroom shots re-planned by hand; the outfit picture keeps build and
  clothes only.
- **Result:** the transcript matches the script but for one clip; the shoe is picked up
  once; the bedroom face holds.

### Round 1: the work-outfit picture and the garage (28 Nov)

- **Found:** the garage face still changed. Lila's work-outfit picture showed a younger
  face than her face picture and pulled every keyframe drawn from it.
- **Changed:** the outfit picture redrawn from the face picture (a second take, since the
  sheet's "collarless" blouse contradicted every keyframe); nine garage keyframes
  redrawn; Ryder's sleeve close-up re-planned as plain listening. In the app: a scene's
  choices carry over to a replaced pick; an insert may carry its line in the sound
  sentence; the limiter's ceiling lowered to about -3 dB after the encoder pushed a peak
  to 0 dB.
- **Result:** Lila reads as one woman through the garage. The pace has not moved, as
  expected: nothing in this round touched the storyboard.

### Round 2: pace (28 Nov, in progress)

- **Gap:** our median shot is 3.9 s against 1.5 s. Thirty of 34 clips hold one view.
- **Change:** re-storyboard the episode as fewer shots of 4 to 8 s with the views named
  inside each, so the cut planner cuts every 1.5 to 3 s. Two rules added to
  `pace.COVERAGE_RULES` first: the speaker of a line is in the shot that carries it, and
  a prop is picked up once and put down once in a scene.
- **First try, `rev_345676a2cbb6`: a copy.** It published with no shot changed. The
  model handed the 34 old shots back and the gate excused them, because coverage was
  only checked on shots a revision changed. Fixed: targeted shots are held to the rules,
  and the coverage check refuses a line cued in a shot its speaker isn't in.
- **Second try, `rev_48ba01523930`:** asked again with the stricter gate.

## Open gaps, in the order they will be taken

1. Pace (round 2).
2. Ryder's hair between shots; Lila in the office.
3. The end of a clip drifting (127 s): trim, or a shorter clip.
4. Lines of 7 words on average against about 5: a script change, the owner's call.
5. Render size (512x896 against 720x1280): time, not craft.
