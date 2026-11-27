# Heartbeats of Desire, episode 1 before rendering: the frames are close, the clips are not brisk, and a third of the lines break mid-phrase

Date: 2026-11-26 (board as of 27 Nov 04:38 UTC).

## Summary

Every one of the 34 shots of revision `rev_e2231b5e06ad` now has an approved keyframe, 41
in all. Read as a sheet they look like one film: the three leads, their clothes and the
three rooms hold from shot to shot, and 26 of 34 shots are close singles, over-the-shoulder
shots or inserts. Three things stand between this board and a brisk episode.

1. **The cuts are 4 s holds.** 28 of 36 planned cuts are exactly 4 s; the median is 4.0 s
   against a target of 2.0 s. The prompts agree with the plans to the tenth of a second:
   plan and prompt are consistent with each other and neither is brisk.
2. **13 of 33 lines are spoken across two or three clips, cut mid-phrase** ("across a |
   canvas."), each clip a separate generation with a second of padding after its words.
   Six fragments are one or two words in 0.1 to 0.7 s, and one word is spoken twice.
3. **Seven shots are not really planned.** Two are held on stale plans and still show the
   old revision's two-shots; five garage shots fell back to a one-line plan when the
   planner's calls were refused for lack of credits, and their keyframes have both women
   looking the same way.

Ranked: re-plan and redraw the seven shots; stop lines crossing a cut by less than a word;
make the storyboard shot the clip and cut inside it; then shorten the lines.

## What was reviewed

| | Heartbeats ep 1, revision `rev_e2231b5e06ad` |
| --- | --- |
| Storyboard | 34 shots, 120 s, written 26 Nov under `pace.COVERAGE_RULES` (shots of 3 to 6 s); story pace brisk |
| Cut plans | 34: 27 written by the planner, 5 one-cut fallbacks (`source: single`), 2 failed and held |
| Keyframes | 41 approved, one per cut; 22 drawn at 1536x2720 before 27 Nov, 19 at 1024x1824 since |
| Video prompts | 32 current, 2 out of date (the held shots); 1 current with a failed check |
| Clips | none rendered for this storyboard (rendering is off on run `run_937395b5430a`) |

Method: the storyboard and lines from `GET /v1/drama-packages/pkg_b25416cb47fc`; plans,
approvals, checks and video prompts from `.../keyframes`; the approved keyframes read by
eye from four contact sheets (not kept: the command is at the end). Nothing was rendered,
so there is no cut list or loudness: pace is read from the plans, speech from the plans and
the prompts' text. This follows
[the plan review of the same day](2026-11-26-heartbeats-ep1-restoryboard-plan-review.md);
its findings 1 to 3 still stand and are measured again here.

## Headline numbers

| | This board | Target (brisk) |
| --- | --- | --- |
| Storyboard length / clip length | 120 s / 145 s (25 s of padding) | |
| Shots / planned cuts | 34 / 36 (the two held shots left out) | |
| Median / mean cut | 4.0 s / 3.6 s | median 2.0 s or less |
| Cuts of 4 s or more | 28 of 36 (78%) | over 4 s: 10% or less |
| Shots cut inside the clip | 4 of 32 | |
| Silence after the last word of a clip | 1.1 s on average; 1 s or more in 20 of 32 | cut on the word |
| CU + MCU + OTS, by storyboard | 26 of 34 (76%) | 60% or more |
| Same, as the keyframes are drawn | about 21 of 34 (five drawn wider than asked) | |
| Words per line / lines over 8 words | 7.0 / 12 of 33 (36%) | 6 or less / 15% or less |
| Lines spoken across clips | 13 of 33 | |
| Line parts spoken by someone off screen | 14 of 46 | |
| Keyframe check score | 100: 18, 75 to 92: 13, 50: 3, 25: 2 | |

## The keyframes

**What holds.** Derek's face, beard and white undershirt; Lila's ivory blouse and charcoal
trousers, her bun with the pencil from the office on, the dark red bag; McKenna's copper
bob and rust sweater; the bedroom's canvas and windows, the office, the garage's brick,
blue cabinets and navy car. Inserts are clean (shoe and rug, hand on the print stack, hands
on the fender, the card in the bag). The card's print is readable in sh_01_03_06 and
sh_01_03_16. The 1024 px keyframes are not visibly softer than the 1536 px ones at sheet
size.

**What to redraw.**

| Shot | What is wrong | Why |
| --- | --- | --- |
| sh_01_02_02 (2 cuts) | A two-shot and a three-shot of Lila standing with McKenna and Derek; the storyboard is now a close single of Lila behind the desk. Lila is a different woman in cut 2 | Old revision's keyframes; the plan failed ("a shot has 1 to 5 sub-shots") and the shot is held |
| sh_01_03_03 (3 cuts) | Two-shots, an extreme close-up and a handshake the storyboard no longer has; planned 9 s for a 4 s shot | Old plan; the re-plan was refused (MCPHub 402, no credits) and the shot is held |
| sh_01_03_08 | Ryder looks into the lens with her arms crossed | One-cut fallback plan: the plan is the storyboard's one sentence, with no placement or eyeline |
| sh_01_03_09 to sh_01_03_11 | Lila, Ryder and Lila again, all waist-up, all facing frame right against the same wall and cars: they do not look at each other | Same fallback |
| sh_01_03_05 and sh_01_03_15 | Ryder faces right toward "Lila off frame right"; Lila, next, faces right toward "Ryder offscreen right" | The two plans contradict each other |
| sh_01_03_15 | A full-figure profile, mid-stride; asked for a medium close-up of her taking her hand off the fender | Drawn wider than planned; approved at 75 |
| sh_01_03_02 | Ryder full figure, seated on the creeper; asked for a medium close-up as she rises | Drawn wider than planned |
| sh_01_01_04 | The same frame as sh_01_01_08 before it (Lila, same size, same pose): two shots read as one 8 s hold | The check said so (score 50); approved because the run approves failed draws |
| sh_01_01_05 | Lila left and Derek right, from the window side: every single before it has Derek on the left looking right | The plan crosses the line on the scene's last shot |
| sh_01_03_01 cut 2 | Ryder lies flat on the floor beside Lila rather than rolling out from under the car | Score 25, approved |
| sh_01_03_13 | The hands are clasped by the door, not tracing the fender as in the insert before | Score 50, approved |

**Ryder's hair changes from shot to shot**: pulled back smooth in sh_01_03_02, 08, 13 and
05, loose curls in sh_01_03_10, 12 and 16. At 4 s a shot, alternating with Lila, it reads
as two women. Lila's face drifts once (sh_01_01_07 cut 2, younger and rounder).

**The office is flat.** White walls, even light, no depth behind anyone: the study's
"separate people and worlds with light" applies to scene 2 as it stands. The bedroom
(window light from one side) and the garage (daylight against brick) are better.

## Pace

The story is set to brisk: "a new setup comes every two or three seconds in dialogue", a
listener held for 1.5 s at most (`pace.RULES`). What is planned:

- **31 clips of 4 s and one of 5 s** (and the two held shots at 7 and 9 s). H3 renders no
  clip under 4 s, and the storyboard's shots are 3 to 4 s, so 18 shots are padded (16 by a
  second) and the planner cut inside only four.
- **A listener holds the frame far past 1.5 s.** sh_01_03_14: Lila hears one word
  ("canvas.", 0.5 s) and the frame stays on her for 3.5 s more. sh_01_02_08: 1.7 s of
  McKenna's voice over the bag, then 2.3 s. sh_01_01_05: "I promise." takes 0.8 s of 4 s.
  sh_01_03_02: "Sorry about that." ends at 1.6 s of 4 s.
- **The silence trim will take some of this back**, but only where the tail is silent and
  still. The prompts give every clip business to the last frame (a palm settles, a head
  turns), so there is movement in the padding and the trim has less to cut.

**Plans and prompts agree.** Of 46 line parts, 45 are in the prompts with the plan's own
words, and every timed on-screen quote that could be matched is within 0.25 s of the plan. Each prompt's timed actions run
to the end of its clip. Two exceptions: sh_01_03_11 says Ryder's "A person" at 3.2 to 4 s
where the plan has 2.3 to 3.0 s; sh_01_03_04's prompt does not quote Ryder's line at all
(the check caught it and the shot is held). So the prompts will produce what the plans
ask. The plans do not ask for a brisk episode.

## Speech

### Lines spoken across clips

A line the storyboard cuts across shots is spoken in parts, each part by a different
generation. The 13:

| Speaker | Parts (shot: words) |
| --- | --- |
| Derek | 01_01: "You know, in the" / 01_06: "history of humankind, 'I've been thinking' is never" / 01_02, off screen: "followed by good news?" |
| Lila | 01_03, off screen: "It should be. You" / 01_08: "tell dad jokes, but dirty." |
| Lila | 01_04: "Point proven. No. I would be late for work." / 01_09, off screen, 0.1 s: "work." |
| Lila | 02_01: "I don't even remember" / 02_02: "sex. Here, draw me a diagram." |
| Derek | 02_03, off screen, 0.2 s: "I" / 02_04: "have the perfect idea for that gallery show. A car!" |
| Derek | 02_05, off screen, 0.2 s: "An" / 02_06: "amazing one! Something that pulls attention. Make it happen!" |
| McKenna | 02_07: "I'm gonna figure out who you're sleeping" / 02_08, off screen: "with. Nothing gets past me." |
| Lila | 03_03: "I'm the curator of an art gallery. We're showcasing traditional" / 03_08, off screen: "painting techniques, and my boss wants a car." |
| Lila | 03_10, off screen, 0.2 s: "No." / 03_11: "I guess I'm an executive aesthete." |
| Ryder | 03_11, off screen, 0.7 s: "A person" / 03_12: "highly sensitive to art and beauty. Come here." |
| Ryder | 03_04: "Look at the curves, the way the" / 03_13: "lines glide. Soft and deliberate, like brushstrokes across a" / 03_14, off screen, 0.5 s: "canvas." |
| Ryder | 03_05: "Exactly. You must see beauty" / 03_15, off screen: "in this." |
| Lila | 03_06: "Let me know which cars are available. I" / 03_16: "look forward to your call." |

What this will sound like:

- **Breaks fall inside a phrase**, after "in the", "is never", "You", "the way the",
  "across a", "sleeping", "I". The parts are split by the share of the line's time in each
  shot, not at a comma or a full stop, though all but two of the lines have one within two
  words of the cut ("It should be. | You tell dad jokes").
- **Each part is its own take of the voice.** Nothing ties the pitch and pace of "across
  a" to "canvas." in the next clip, and the first clip pads a second after its last word.
- **Six fragments cannot be spoken as planned**: "work." in 0.1 s, "I", "An" and "No." in
  0.2 s, "canvas." in 0.5 s, "A person" in 0.7 s. Each is a whole off-screen-voice clip
  for one or two words.
- **"work." is spoken twice.** sh_01_01_04 has the whole line, "…late for work.", and
  sh_01_01_09 opens with "work." again. `plans._line_part` rounds the first shot's share
  up to all nine words and then gives the last shot one word regardless.

### Off-screen voices

14 parts are spoken by someone not in the shot. The writer puts each in the `sound`
sentence as an off-screen voice with its seconds, and the person in frame "listens in
silence, mouth closed": the form is consistent in all 14. Whether H3 keeps the listener's
mouth shut, and whether Derek's off-screen voice in sh_01_01_02 is the voice of sh_01_01_06,
has not been tested: no clip of this storyboard has been rendered with the speech check.
The earlier review's finding 2 asked for exactly that test.

### Speed

Most parts run at 2.2 to 3.1 words a second. Three are fast: McKenna's "Nope. You almost
had me. Secret sex. It's someone I know." (11 words in 3.0 s) and the pair in sh_01_03_09,
"Not a car person?" and "I mean, I can drive." (3.6 words a second each, 0.3 s apart).

## What to change in Draper

In order of impact:

1. **Re-plan the seven shots, then redraw them.** sh_01_02_02 and sh_01_03_03 (held), and
   sh_01_03_08 to sh_01_03_12 (`source: single`, from the planner's 402 refusals; later
   calls have gone through). A fallback plan should not look finished: the board shows
   these five as `failed` but the run drew and approved their keyframes. In
   `shots/episode.py`, hold a shot whose plan is the fallback of a failed call, as it holds
   one with problems.
2. **Do not approve what the check failed on framing.** Five keyframes at 25 or 50 were
   approved because the run's `approve_failed` is on. Keep it for retries that ran out, but
   list them on the board as "approved unchecked" so they are redrawn before a render:
   sh_01_01_04, sh_01_03_01 cut 2, sh_01_03_08, sh_01_03_13, sh_01_03_06.
3. **Keep a line's parts speakable** (`shots/plans.py`, `_line_part` and `_line_shares`):
   - a part under 0.4 s goes to the neighbouring shot whole (the earlier review's sliver
     rule, for `hard_checks.check_timeline`); that removes all six fragments;
   - never give two shots the same word (the clamp `a = min(n - 1, …)`);
   - split at the nearest punctuation within two words of the time share.
4. **Storyboard shots are clips of 4 to 8 s, cut inside** (`pace.COVERAGE_RULES`,
   `check_coverage`; the earlier review's change 2, still open). Today a 3 s shot becomes a
   4 s clip with one cut. With 4 to 8 s shots the same lines sit in half as many clips, the
   planner has room for cuts of 1.5 to 3 s, and most of the 13 split lines stay inside one
   generation.
5. **Check eyelines across a cut.** `planner_rules()` places people in a shot but nothing
   compares two neighbouring singles. Add to the plan check: in an exchange, consecutive
   singles of the two speakers face opposite sides (sh_01_03_05 and sh_01_03_15 both say
   "off frame right"), and a two-shot keeps the side each person had (sh_01_01_05).
6. **One hair for Ryder.** Her look sheet should fix it (`CastStep`,
   `distinguishing_features`), and the keyframe check should compare hair with the look
   picture as it does clothes.
7. **Shorten the lines** (`ScriptStep.directive`): 7.0 words a line and 36% over 8 words,
   unchanged since the study. Shorter lines are what make change 4 work.
8. **Light the office** (the style brief, `StoryboardStep`): a window side and a darker
   wall behind each single.

## Measuring the next render

| Metric | Old render (run of 24 Nov) | This board, as planned | Target (brisk) |
| --- | --- | --- | --- |
| Median cut | 2.8 s | 4.0 s | 2.0 s or less |
| Cuts over 4 s | 26% | 3% over, 78% at exactly 4 s | 10% or less |
| CU + MCU + OTS | 16% | 76% by storyboard, about 62% as drawn | 60% or more |
| Wide two-/group shots | 55% | 12% | 30% or less |
| Words per line / over 8 words | 7.0 / 36% | 7.0 / 36% | 6 or less / 15% or less |
| Lines spoken across clips | | 13 of 33 | 0, or only at punctuation |
| Fragments under 0.8 s | | 6 | 0 |

```sh
# The board and the package (server on port 8000)
curl -s http://127.0.0.1:8000/v1/drama-packages/pkg_b25416cb47fc > pkg.json
curl -s http://127.0.0.1:8000/v1/drama-packages/pkg_b25416cb47fc/keyframes > board.json
# Per cut: the approved take is sub_shots[].approved = [take_id, n]; its picture is that
# take's results[n].file under data/media/. One sheet a scene:
magick montage -label '%t' <files in storyboard order> -tile 6x -geometry 330x587+4+4 sheet.jpg
# After a render: cuts, shot lengths and loudness
python skills/director/scripts/measure.py data/media/episodes/pkg_b25416cb47fc/<run_id>.mp4
```
