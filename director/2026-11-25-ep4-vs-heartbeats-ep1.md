# Shot selection and pace: a mature vertical drama clip vs. Heartbeats of Desire, episode 1

Date: 25 Nov 2026.

## Summary

The reference clip moves almost twice as fast and is shot almost entirely in close
singles. Every line lands on a face, and the power shifts play out in reaction shots.
Heartbeats of Desire episode 1 is mostly full-figure two-shots held for three to seven
seconds, so it reads like a stage play filmed from the audience.

The cause is upstream of the camera. The storyboard planned 16 shots of 5 to 11 seconds
each, all two- or three-shots, most of them locked off. The coverage rules that would have
prevented this (`pace.COVERAGE_RULES`, `hard_checks.check_coverage`) were added after this
storyboard was written. Their numbers (3 to 6 s shots) are also still slower than the clip
(median 1.6 s).

The five lessons, in order of impact:

1. Cover dialogue in close singles and over-the-shoulder shots; keep wides for power and
   movement.
2. Cut every 1.5 to 2.5 seconds, on the line or the reaction.
3. Let the listener's face carry the turn of a scene.
4. Use light, depth of field and wardrobe to separate people and worlds.
5. Write short lines, start inside the conflict, and end on a reversal.

## What was compared

| | Reference clip | Heartbeats of Desire, episode 1 |
| --- | --- | --- |
| File | `~/Downloads/ep4_preview.mp4` (a third-party vertical drama, episode 4 preview) | `data/media/episodes/pkg_b25416cb47fc/run_a18baec10cd5.mp4` (episode run, 23 Nov) |
| Story | A fake-engagement romance: the hero defends his "temp fiancée" from three rivals on a rooftop party, then orders a lavish wedding | Lila ends things with Derek in his bedroom, an office scene with McKenna and Derek, then Lila meets Ryder at the garage |
| Package | n/a | `pkg_b25416cb47fc`, revision `rev_45335e3deb3f`, storyboard written before the coverage rules (commit `f02b2b5`) |

### Method

- **Cuts:** found with ffmpeg scene detection (`select='gt(scene,0.25)'`), then checked by
  eye. Three detections at 45.0 to 45.4 s in the clip were one tracking shot through
  changing light and were merged.
- **Shot size:** classified from a frame at the middle of every shot.
- **Audio:** measured with `volumedetect` and per-second RMS.
- **Our side:** the storyboard and lines come from the package API.

The clip's stills are not kept in the repo (third-party material). Rebuild them locally
with the command at the end. Our contact sheet is
[`heartbeats-ep1-shots.jpg`](heartbeats-ep1-shots.jpg): one frame per shot, labeled with
its shot number, start time and length.

## Headline numbers

| | Reference clip | Heartbeats ep 1 |
| --- | --- | --- |
| Length | 56 s | 114 s (120 s planned) |
| Shots on screen | 29 | 38 (from 16 storyboard shots) |
| Average / median shot | 1.9 s / 1.6 s | 3.0 s / 2.8 s |
| Longest shot | 5.4 s (a held listening close-up) | 6.9 s (a full-figure two-shot) |
| Shots over 4 s | 1 | 10 |
| Close-up or medium close-up | 16 of 29 (55%) | 5 of 38 (13%) |
| Over-the-shoulder or close two-shot | 4 (14%) | 1 (3%) |
| Full-figure or wide two-/group shots | 8 (28%) | 21 (55%) |
| Inserts (a hand, a prop) | 1 (the phone) | 0 |
| Locations | 1 (rooftop terrace, plus the doorway inside) | 3 (bedroom, office, garage) |
| Dialogue lines | about 20 visible, about 5 words each | 33, 7 words on average, 12 over 8 words, longest 18 |
| Audio | mean -16.5 dB, peak -1.6 dB, music bed about -28 dB under speech | mean -17.8 dB, **peak 0.0 dB** (at full scale: check for clipping) |
| Picture | 720x1280, 24 fps | 512x896 (fast draft render), 24 fps |

## Shot by shot

### Reference clip (29 shots, 56 s)

Size abbreviations: CU close-up, MCU medium close-up, MS medium shot, OTS
over-the-shoulder.

| # | Start | Length | Size and subject | Line on screen (whose) | What it does |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.0 | 1.2 | MCU, rival in white (warm interior light) | "Landon." (rival) | Opens mid-scene on a speaker |
| 2 | 1.2 | 2.3 | From behind: the hero walks in on the women | "We were just congratulating her..." (rival, off screen) | Entrance; the line plays over his back |
| 3 | 3.5 | 3.4 | MCU, the hero | "Sounded more like you were cornering my fiancée." | The challenge |
| 4 | 6.8 | 2.1 | MCU, blonde rival | "No, no, we were just chatting..." | Backpedal |
| 5 | 8.9 | 1.2 | Close two-shot, the hero beside the heroine | | Shows alliance in one frame |
| 6 | 10.1 | 1.5 | CU, the heroine | "Anyone giving you a hard time?" (hero, off screen) | The line lands on the listener |
| 7 | 11.6 | 3.2 | OTS profile two-shot | "No. They were just teaching me how to be a 'temp fiancée.'" | Her jab |
| 8 | 14.8 | 0.6 | CU, the hero: a half-smile | | Micro-reaction |
| 9 | 15.4 | 0.8 | CU, the hero: looks down | | Micro-reaction (background jumps to warm interior: continuity slip) |
| 10 | 16.2 | 1.1 | CU, the hero | "'Temp'?" | One-word turn |
| 11 | 17.3 | 1.8 | MCU, rival in white: shocked | "Evelyn is my fiancée." (hero, off screen) | Statement played on the person it hits |
| 12 | 19.1 | 2.4 | MS two-shot: the couple side by side facing the rivals | "Anyone who disrespects her disrespects me." | Power framing: united front |
| 13 | 21.5 | 1.6 | Group: three rivals | | Group reaction |
| 14 | 23.0 | 1.2 | CU, the hero: hard look | | Threat |
| 15 | 24.3 | 1.7 | Group: three rivals | "Are we clear?" | Button on the losers' faces |
| 16 | 26.0 | 1.3 | Wide two-shot at the railing (rivals gone) | | Transition to the private beat |
| 17 | 27.2 | 5.4 | MCU, the heroine, city lights behind | "I don't care what they say." | The one long hold: the emotional turn |
| 18 | 32.7 | 3.7 | Wide profile two-shot at the railing | "You're my fiancée." / "As long as you hold that title, no one gets to mess with you." | Distance between them in frame |
| 19 | 36.4 | 3.8 | MCU, the heroine | "You're a pretty good actor." | Her probe |
| 20 | 40.1 | 1.2 | OTS on the hero | "Professionalism." | One-word deflection |
| 21 | 41.3 | 3.8 | MS two-shot | "It's getting cold. I'm going in." | Exit line |
| 22 | 45.0 | 1.2 | Tracking from behind: she walks inside | | Movement carries the transition |
| 23 | 46.2 | 0.9 | OTS from behind the hero onto Felix | "Felix, get me a wedding planner." | New beat: an order |
| 24 | 47.1 | 2.1 | MCU, Felix adjusting his glasses | "It's past eleven." | Comic pushback |
| 25 | 49.2 | 1.0 | CU, the hero, mouth open | "I said now." | Snap |
| 26 | 50.3 | 0.6 | Insert: phone in Felix's hand | | Action beat in a cut |
| 27 | 50.9 | 1.5 | MCU, Felix on the phone | | Obeys |
| 28 | 52.4 | 1.7 | CU profile, the hero | "I'm giving Evelyn the wedding of the century." | Cliffhanger reversal |
| 29 | 54.1 | 2.0 | Hold on the hero, then an end card | | "To be continued" |

### Heartbeats ep 1 (38 shots, 114 s)

| # | Start | Length | Size and subject | Scene |
| --- | --- | --- | --- | --- |
| 1 | 0.0 | 1.6 | Wide two-shot: Derek in bed, Lila standing | Bedroom |
| 2 | 1.6 | 5.1 | MS, Derek in bed | Bedroom |
| 3 | 6.8 | 2.8 | MS two-shot | Bedroom |
| 4 | 9.5 | 0.9 | Wide: Lila crosses the room | Bedroom |
| 5 | 10.5 | 2.0 | MS, Derek | Bedroom |
| 6 | 12.5 | 1.6 | Full figure: Lila bends for her shoes | Bedroom |
| 7 | 14.0 | 1.2 | Wide two-shot | Bedroom |
| 8 | 15.2 | 3.9 | MS, Lila at the window | Bedroom |
| 9 | 19.1 | 2.2 | Close two-shot | Bedroom |
| 10 | 21.4 | 4.8 | Wide two-shot | Bedroom |
| 11 | 26.2 | 3.1 | MS, Derek on the bed's edge | Bedroom |
| 12 | 29.3 | 5.2 | MS two-shot: Lila leans in | Bedroom |
| 13 | 34.5 | 1.9 | Wide office two-shot | Office |
| 14 | 36.3 | 4.4 | MS, Lila at the desk | Office |
| 15 | 40.7 | 0.4 | CU, McKenna | Office |
| 16 | 41.1 | 3.9 | Full-figure two-shot | Office |
| 17 | 45.0 | 3.4 | Full-figure three-shot | Office |
| 18 | 48.4 | 2.5 | MS, Lila | Office |
| 19 | 50.9 | 4.8 | Full figure, Derek | Office |
| 20 | 55.8 | 0.5 | Wide three-shot | Office |
| 21 | 56.3 | 4.9 | Full figure, McKenna | Office |
| 22 | 61.2 | 2.0 | MS, Lila packing her bag | Office |
| 23 | 63.2 | 1.0 | Wide: garage, Lila enters | Garage |
| 24 | 64.2 | 1.2 | MCU, Lila | Garage |
| 25 | 65.5 | 2.1 | Wide: Ryder on the creeper | Garage |
| 26 | 67.6 | 2.9 | MS, Ryder sitting up | Garage |
| 27 | 70.5 | 6.4 | Full-figure two-shot | Garage |
| 28 | 76.9 | 1.6 | MCU, Ryder | Garage |
| 29 | 78.5 | 1.9 | MS, Lila | Garage |
| 30 | 80.4 | 2.5 | Full-figure two-shot | Garage |
| 31 | 83.0 | 3.1 | CU, Lila | Garage |
| 32 | 86.0 | 3.8 | Full-figure two-shot | Garage |
| 33 | 89.8 | 6.9 | MS two-shot over the car | Garage |
| 34 | 96.7 | 1.0 | Full-figure two-shot | Garage |
| 35 | 97.7 | 3.1 | Full-figure two-shot | Garage |
| 36 | 100.8 | 2.4 | MS, Lila | Garage |
| 37 | 103.2 | 6.7 | Full-figure two-shot | Garage |
| 38 | 109.9 | 4.1 | MS, Lila alone | Garage |

What the storyboard asked for (`rev_45335e3deb3f`, episode 1): 16 shots of 5 to 11 s
(7.5 s on average). All 16 are two- or three-shots ("Medium two-shot", "Medium-wide office
two-shot", "Tight two-shot"). Most cameras are "Locked" or "Static", each shot carrying 1 to
3 lines. The cut planner split these into 38 pieces, but a piece of a two-shot is still a
two-shot. The render can't be faster or closer than the plan.

## Findings

### 1. Coverage: close singles, not two-shots

The clip covers an exchange as shot / reverse shot: a close single of whoever speaks, an
over-the-shoulder to keep the other person present, then back. Wide and group frames are
reserved for three jobs:

- **an entrance** (shot 2);
- **a power picture** (shot 12: the couple shoulder to shoulder facing the rivals; shots 13
  and 15: the rivals as a unit);
- **a move to a new beat** (shots 16 and 22).

That is 8 wide frames out of 29, each one doing something a close-up can't.

Ours carries the exchange in full-figure two-shots: shots 27, 30, 32, 34, 35 and 37 in the
garage are all the same setup, two women standing at full height. On a 9:16 phone screen a
full-figure two-shot leaves each face about a tenth of the frame's height, so expressions
don't read. Nothing tells the viewer whose moment it is.

**Lesson:** medium close-up is the default size for dialogue in a vertical frame. A
two-shot has to earn its place by showing a relationship in space (alliance, distance, a
standoff).

### 2. Rhythm: cut on the line and on the reaction

The clip's median shot is 1.6 s. A cut lands on nearly every line, and often a second cut
lands within the line, onto the listener. Speed comes from cutting, not camera moves: most
shots are held or barely drifting. The two exceptions are the walk-off tracking shot and
the one long hold (shot 17, 5.4 s), which is the emotional pivot. One deliberate hold in a
fast clip makes that moment heavier.

Ours averages 3.0 s, with ten shots over 4 s. The long ones (27, 33 and 37, at 6 to 7 s)
are conversations held in a two-shot after the line is over: dead air the eye notices.

**Lesson:** at a brisk pace a shot is 1.5 to 2.5 s. Anything over 3 s needs a reason: a
walk, a reveal, or the scene's single emotional hold.

### 3. Reaction: the listener's face carries the turn

The clip's strongest beats aren't on the speaker:

- shot 6 ("Anyone giving you a hard time?") is on her eyes;
- shot 11 ("Evelyn is my fiancée.") is on the rival who just lost;
- shot 15 ("Are we clear?") is on the three faces taking the hit;
- shots 8 and 9 are 0.6 s and 0.8 s micro-reactions (a half-smile, a glance down) before
  the hero's one-word turn "'Temp'?".

Ours has almost no reaction cutaways. The listener is in the same wide frame as the
speaker, so when Lila says "I think we should stop having sex" we never cut to Derek
taking it.

**Lesson:** for every line that turns the scene (a reveal, a threat, an insult, a
confession), plan a close-up of the person it lands on. Our planner rule caps listener
cuts at 1.5 s, which matches the clip's micro-reactions. But the storyboard should plan
reaction shots at the turns, not only allow them.

### 4. Look: light, depth and wardrobe separate people and worlds

The clip:

- **Light:** one night location; city lights behind every face, blurred by shallow focus;
  cool blue for the night, warm for the skin. Every face separates from its background.
- **Wardrobe:** the heroine is the only one in a black leather jacket. The three rivals
  are in cream, beige and white. One glance tells you who doesn't belong.
- **A signature detail:** the hero's dark red tie runs through every shot of him.

Ours is evenly lit daylight with deep focus: the bed, the art on the wall and the window
are all as sharp as the faces. Palettes are soft neutrals in all three locations, so the
bedroom, office and garage feel like one catalog. Lila wears the same cream blouse
throughout (correct for the story), but no costume or color contrast says who holds power
in each scene.

**Lesson:** the style brief should ask for:

- motivated light with contrast (a window, a lamp, city lights);
- shallow depth of field on singles;
- a palette per world;
- one visual signature per lead (a color, a garment, an accessory) that contrasts with
  whoever opposes them.

### 5. Writing: short lines, start in the conflict, end on a reversal

The clip's lines average about 5 words, and the key ones are one to three: "'Temp'?",
"Professionalism.", "Are we clear?", "I said now." Short lines are what make 1.6 s shots
possible: a line fits inside a shot, and the next shot can be the reaction. The clip opens
on a confrontation already under way ("Landon." on a rival's face) and ends on a decision
that raises the stakes ("I'm giving Evelyn the wedding of the century.").

Our episode's lines average 7 words; 12 of 33 are over 8, and the longest is 18 ("You
know, in the history of humankind..."). The opening is a quiet bedroom conversation that
starts with "I've been thinking." The episode ends on "Wow." over Lila leaving the garage:
a mood, not a hook.

**Lesson:**

- **Lines:** most under 8 words, with the punchlines and turns at 1 to 3.
- **Opening:** start inside the conflict within the first 3 s.
- **Ending:** end on a line or image that changes what the next episode is about.

### Also worth noting

- **One location per beat.** The clip spends 56 s in one place and changes the beat by who
  is present (rivals, then the couple alone, then Felix). Ours spends 120 s across three
  locations with three casts, so each scene has to re-establish itself. At 30 to 55 s per
  scene, the re-establishing shots eat time a close-up could use.
- **Picture quality.** Our run is a fast 512x896 draft, so softness is expected; judge the
  final look at full size.
- **Audio.** Our mix peaks at full scale (0.0 dB). Compare the clip's -1.6 dB, and check
  the loudness pass for clipping.

## Where the clip goes wrong (don't copy)

- **Look-alike cast.** The rival in white has nearly the heroine's face and hair, and
  Felix reads as the hero in glasses. Distinct faces matter more when shots are this
  short.
- **Background jumps.** Shots 8 to 9 and 11 swap the hero and the rival into a warm
  interior with curtains in the middle of a terrace exchange.
- **Face drift.** The hero looks softer at 4 s and 54 s than in the angular "Are we clear?"
  close-up at 23 s.
- **Sideways end card.** "TO BE CONTINUED" runs vertically down the right edge, cut off and
  over the actor, after a lens-flare wipe.
- **Mid-frame subtitles.** They sit at chest height and cover wardrobe in the two-shots; a
  fixed lower third is cleaner.

Our keyframe consistency work (reference-anchored faces, continuity checks against
neighboring shots) already guards against the first three. Keep it as cuts get shorter.

## What to change in Draper

In order of impact:

1. **Re-storyboard Heartbeats under the current coverage rules, and re-measure before
   tuning further.** The rules in `pace.COVERAGE_RULES` and `hard_checks.check_coverage`
   (commit `f02b2b5`) address findings 1 and 2 but have never run on this story. Run a
   revise on the storyboard for episode 1, render it, and rerun the measurements below.
2. **Tighten brisk.** `COVERAGE_RULES` says shots are 3 to 6 s. For brisk, make it 1.5 to
   3 s, at most one line per shot, with a single longer hold (up to 5 s) allowed per scene
   for its emotional turn. Measured and slow burn keep longer shots. This means making
   `COVERAGE_RULES` depend on the pace, as the reaction cap already does.
3. **Plan reactions at the turns.** Add to the storyboard rules: each beat that turns the
   scene gets a close-up of the person it lands on. Add a creative-review check
   (`"reaction"`, minor) when a scene's turning line has no reaction shot.
4. **Write for short shots.** The script step's rules: most lines under 8 words; split long
   speeches across a reaction; open inside the conflict; end the episode on a reversal
   line. A soft check could flag lines over 12 words in brisk stories.
5. **Ask the style brief for separation.** In the storyboard's style brief: motivated
   contrast light, shallow depth of field on singles, a palette per location, and a visual
   signature per lead. In the cast sheet: costume contrast between opposing characters.
6. **Fewer locations per episode in brisk stories.** Prefer one or two locations, changing
   the beat by who enters or leaves. This is an outline-step guideline, not a hard rule.

These also belong in a story template (`docs/templates.md`): a "fake engagement, public
defense" template learned from this kind of drama would carry the coverage, rhythm and
reaction pattern above into a new story.

## Measuring the next render

Track these for every episode run; this clip is the bar:

| Metric | Clip | Ep 1 now | Target (brisk) |
| --- | --- | --- | --- |
| Median shot length | 1.6 s | 2.8 s | 2.0 s or less |
| Shots over 4 s | 3% | 26% | 10% or less |
| CU + MCU + OTS share | 69% | 16% | 60% or more |
| Wide two-/group shots | 28% | 55% | 30% or less |
| Words per line (average) | about 5 | 7.0 | 6 or less |
| Lines over 8 words | about 10% | 36% | 15% or less |

Commands used (run from the repo root):

```sh
V=~/Downloads/ep4_preview.mp4   # or data/media/episodes/<pkg>/<run>.mp4
ffprobe -v error -show_entries format=duration:stream=codec_type,width,height,r_frame_rate -of compact "$V"
# Cuts (then check by eye: light changes inside one shot also trigger it)
ffmpeg -hide_banner -i "$V" -vf "select='gt(scene,0.25)',showinfo" -f null - 2>&1 | grep -o "pts_time:[0-9.]*"
# Loudness
ffmpeg -hide_banner -i "$V" -af volumedetect -vn -f null - 2>&1 | grep -E "mean_volume|max_volume"
# One frame per shot: for each cut segment (a, b), grab the frame at (a + b) / 2
ffmpeg -v error -ss <mid> -i "$V" -frames:v 1 -vf scale=160:-1 shot_NN.png
magick montage -label "NN start +len" shot_*.png -tile 10x -geometry +2+2 shots.jpg
```
