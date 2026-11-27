# Heartbeats of Desire, episode 1, re-storyboarded: the plan is right, the plumbing under it is not

Date: 2026-11-26.

## Summary

Revision `rev_e2231b5e06ad` re-storyboards episode 1 under the coverage rules: 34 shots
(from 16), 3 to 5 s each, 26 of them close singles, over-the-shoulder shots or inserts. On
paper it hits the study's shot-size targets and most of its rhythm targets. Read before
rendering, three things stand in its way, all below the storyboard:

1. **H3 renders no clip under 4 s.** 16 of the 34 shots are 3 s. Each becomes a 4 s clip
   with a second of padding, so the cut plan, not the storyboard, has to make the short
   shots: a 4 to 8 s shot cut into sub-shots inside one clip.
2. **14 lines are spoken by someone not in the shot.** That is the reaction coverage the
   study asks for, and it is exactly what the plan rule `OFF_SCREEN_LINE` refuses as a
   hard problem since the McKenna defect. As the rules stand, a third of the episode holds.
3. **The old revision's cut plans and keyframes came along.** Until this morning a plan a
   revision outdated was held, not re-planned, and a locked plan was never outdated; four
   garage shots still carried 6 to 11 s plans and wide two-shot keyframes for what are now
   inserts and singles. Fixed in code today; the run re-plans and redraws them.

Also found and fixed on the way: a line cut across shots was quoted whole in its first shot
and not at all in the next, so H3 mouthed nonsense (13 lines in this episode are split);
the planner adds people the action mentions to singles; and it cuts inside lines.

## What was reviewed

| | Heartbeats ep 1, revision `rev_e2231b5e06ad` |
| --- | --- |
| Storyboard | 34 shots, 120 s, written 26 Nov by a revise run of all three scenes under `pace.COVERAGE_RULES` (3 to 6 s shots, one wider placing shot, singles and over-the-shoulder for the exchange) |
| Cut plans and keyframes | As on the board when run `run_937395b5430a` was paused: 21 approved keyframes, 13 of them carried over from `rev_45335e3deb3f` |
| Lines | 33, as before: 7.0 words on average, 12 over 8 words (the revision touched the storyboard only) |

Method: the storyboard, cues and lines from the package API; the plans and keyframes from
the keyframe board; a contact sheet of the approved keyframes built with `magick montage`
from `data/media/keyframes/<package>/<take>_<n>_thumb.jpg` (not kept in the repo). Two
probe jobs against the H3 route for the clip floor.

## The storyboard against the checklist

| Check | Scene 1 (bedroom, 10 shots, 35 s) | Scene 2 (office, 8 shots, 30 s) | Scene 3 (garage, 16 shots, 55 s) |
| --- | --- | --- | --- |
| Opening inside the conflict | A medium-wide two-shot, then straight to Derek's line: yes | A medium-wide single of Lila with McKenna's "You had sex." off screen: yes, and the line lands on Lila | A medium-wide single of Lila walking in on "Excuse me!": placing shot, fine |
| Dialogue in alternating singles | Yes: Derek MCU, Lila MCU, OTS on Derek, Derek MCU, Lila MCU, Lila CU, Derek MCU | Yes: Lila, McKenna, Derek in the doorway, Lila, Derek, McKenna | Yes, with two OTS shots at the fender |
| Reaction at the turn | "I think we should stop having sex" is on Lila (the speaker); the reaction is the OTS on Derek for "Point proven": acceptable | "You had sex." plays on Lila: yes | "Exactly. You must see beauty in this." is on Ryder; Lila's "I do." gets its own MCU: yes |
| Length | 3 to 5 s, one or two lines a shot; no hold marked for the turn | Same | Same; the insert of the hands carries a 3.2 s line |
| Wides doing a job | Opening and closing two-shots only | None after the opening | None after the opening |
| Variety | No two consecutive shots share size and people | Same | Same |
| Ending | "I promise." in a two-shot: a button | Insert of the bag closing: a button | "Wow." on Lila at the door: a mood, not a hook (unchanged from the study) |

Shot sizes over the episode: 16 medium close-ups, 6 close-ups, 4 over-the-shoulder, 4
inserts, 4 two-shots or wides. CU + MCU + OTS is 76% (target 60% or more); two-shots and
wides 12% (target 30% or less). Mean shot 3.5 s; the median will be set by the cut plans.

The lines are the one target not met: 7.0 words a line, 36% over 8 words. The revision
was asked for the storyboard only, so this is expected, and it is why 13 lines now run
over a cut.

## Findings

### 1. The clip floor is 4 s, so the storyboard's 3 s shots cannot be rendered as written

Two probe jobs at 512 px, 14 steps: 3 s and 2 s are refused with "the model server could
not accept the job"; 4 s renders. `MIN_CLIP_S` is 4 for that reason. A 3 s storyboard shot
becomes a 4 s clip (`clip_ms` rounds up to whole seconds with a floor of 4), and the extra
second is padding at the end: the person finishes the line and holds. The silence trim
takes some of it back, but a held face is not silence.

**Lesson:** at a brisk pace the storyboard shot is the *clip*, 4 to 8 s, and the cut plan
makes the 1.5 to 3 s shots inside it. The coverage rules should say so: shots of 4 to 8 s
with two to four cuts planned inside, never a 3 s shot. The study's 1.5 to 3 s target is a
sub-shot target.

### 2. Reaction coverage means off-screen lines, which the plan rule refuses

14 cues in this episode put a line in a shot whose speaker is not in it: McKenna's "You
had sex." over Lila's single, Lila's "I don't like you" over Derek, Ryder's "Not a car
person?" over Lila, and so on. That is the listener's face carrying the line, as the study
asks. It is also the McKenna defect: H3 lip-syncs a quoted line to the face it sees. The
rule added after that defect (`plans.OFF_SCREEN_LINE`, a hard problem at any pace) holds
every one of these shots.

The writer already has the other half of the answer: an off-screen line goes in the
`sound` sentence as voice-over, the text says the person in frame listens with their mouth
closed, and the speech check rejects a take where the listener's lips move. What is
missing is evidence that H3 obeys the voice-over form, and a rule that lets it through.

**Lesson:** make `OFF_SCREEN_LINE` advisory when the shot's prompt uses the voice-over
form, and let the speech check be the gate. Before that, render three or four of these
shots (sh_01_02_01, sh_01_03_08, sh_01_03_14) and count how many takes the check accepts.
If H3 mostly fails, the storyboard rule has to change instead: a reaction shot gets the
line's *last word* or a beat of silence, not the whole line.

### 3. Four slivers: a line crossing a cut by 0.1 to 0.2 s

sh_01_01_09 holds 0.1 s of Lila's line before Derek's; sh_01_02_03, sh_01_02_05 and
sh_01_03_10 hold 0.2 s of someone else's. With lines now spoken in parts, a sliver is one
word mouthed by someone off screen in a shot that is otherwise clean: an off-screen-line
problem for nothing.

**Lesson:** the timeline check should snap a cue that crosses a shot boundary by under
0.4 s to the boundary (`validation/hard_checks.py`, `check_timeline`), or the storyboard
step should be told that a line starts and ends inside one shot unless it is cut on
purpose.

### 4. Plans and keyframes carried from the old revision

The contact sheet showed wide garage two-shots approved as the keyframes of sh_01_03_03
(now an MCU single of Lila), sh_01_03_04 (an insert of hands), sh_01_03_05 and sh_01_03_06,
with plans of 6 to 11 s. The revise moves plans and keyframes to the new revision and marks
the changed shots' plans outdated, but the run held on their problems instead of
re-planning, and a locked plan was exempt from the revision check. Both fixed today
(`shots/episode.py`, `shots/staleness.py`): the run re-plans an outdated plan, locked or
not, which withdraws the approvals and redraws the cuts.

### 5. Lines across cuts, and the planner's two slips

Fixed today, listed so the next review knows the baseline:

- `plans.shot_lines` splits a line cut across shots into the words spoken in each, by the
  share of its time; the writer quotes exactly those, mid-sentence when continued, and the
  quote check refuses a prompt that says the speech "carries over" with no words.
- `planner.planned_sub_shots` leaves out a person the planner names who isn't in the shot
  (the action mentions McKenna, so the planner put her in Lila's single), and moves a cut
  that lands inside a line to the line's end instead of refusing the plan.

## What to change in Draper

In order of impact:

1. **Decide the off-screen-line rule** (finding 2). Measure first: render sh_01_02_01,
   sh_01_03_08 and sh_01_03_14 with the voice-over form and the speech check on, two takes
   each, and count the accepted takes. Then either make `OFF_SCREEN_LINE` advisory when the
   prompt uses the voice-over form, or change the storyboard rule so a reaction shot
   carries at most the last word of the line.
2. **Storyboard shots are clips: 4 to 8 s, cut inside** (finding 1). `pace.COVERAGE_RULES`
   and `hard_checks.check_coverage`: shot floor 4 s; at a brisk pace two to four cuts per
   shot planned by the cut planner; the 1.5 to 3 s target moves to the planner rules.
3. **Snap slivers** (finding 3): `check_timeline` moves a cue end or start within 0.4 s of a
   shot boundary onto it.
4. **Shorten the lines** (the study's finding 5): a revise of the script for episode 1
   with the writing rules, then re-measure words per line.
5. **End scene 3 on a hook**, not "Wow." (the study's finding 5).

Then render, measure with `skills/director/scripts/measure.py`, and compare with the study's
table: median shot, shots over 4 s, the size shares, and the speech check's rejection rate
per shot type.

## Measuring the next render

| Metric | Ep 1, old render | Target (brisk) | This storyboard, on paper |
| --- | --- | --- | --- |
| Median shot length | 2.8 s | 2.0 s or less | set by the cut plans; shots are 3 to 5 s, clips 4 s or more |
| Shots over 4 s | 26% | 10% or less | 0% by storyboard; padding makes it higher |
| CU + MCU + OTS share | 16% | 60% or more | 76% |
| Wide two-/group shots | 55% | 30% or less | 12% |
| Words per line (average) | 7.0 | 6 or less | 7.0 (unchanged) |
| Lines over 8 words | 36% | 15% or less | 36% (unchanged) |

Commands (from the repo root, the server on port 8000):

```sh
# The storyboard, plans and lines of episode 1 side by side
curl -s http://127.0.0.1:8000/v1/drama-packages/pkg_b25416cb47fc > /tmp/pkg.json
curl -s http://127.0.0.1:8000/v1/drama-packages/pkg_b25416cb47fc/keyframes > /tmp/board.json
# Approved keyframes, one per cut: data/media/keyframes/<pkg>/<take>_<n>_thumb.jpg
magick montage -label '%t' ... -tile 8x -geometry 150x267+2+2 keyframes.jpg
# Can H3 render a 3 s clip? (no: 502, "could not accept the job")
curl -s https://api.mcphub.com/api/v3/gpu-videos/minimax-h3-ref2va/jobs -H "X-API-Key: $MCPHUB_KEY" \
  -d '{"task":"ref2va","target":{"short_edge":512,"aspect_ratio":"9:16","duration_seconds":3}, ...}'
```
