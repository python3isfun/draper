# Render speed: steps and reference pictures

What makes an H3 Ref2VA clip slow on MCPHub's GPU route, measured on 2026-11-24 with one
shot of *Heartbeats of Desire* rendered six ways, and what the app now does about it.

## Where the time went

Episode 1 was rendered twice over with the fast option (512 short edge). The 18 takes that
rendered without any wait in our own queue took 5 to 19 minutes each, and the time had
nothing to do with the clip's length:

| References sent | Clip | Render time |
| --- | --- | --- |
| 3 | 5 s | 5.3 min |
| 4 | 7 to 8 s | 9.0 to 9.2 min |
| 5 | 7 to 8 s | 10.8 to 11.5 min |
| 6 | 7 s | 14.5 and 18.6 min |

A least-squares fit over the 18 takes gives about 3.3 minutes per reference picture and
nothing per second of video. The route encodes every reference before it starts generating,
and at its default step count each picture costs as much as a sizeable part of the clip.

The route also validates an undocumented request field, `num_inference_steps`, an integer
from 1 to 100, which the app never sent. The job record does not say what the default is.

## The measurement

Shot `sh_01_03_01` (7 s, four cuts, Lila and Ryder in the repair bay), the same seed and the
same prompt every time, rendered at 512 short edge one after another on the GPU, with the
route reporting one job of another account ahead in its queue for each:

| Variant | Steps | References | Reference short edge | Time |
| --- | --- | --- | --- | --- |
| route default | default | 6 | 768 | 13 min 41 s |
| low | 14 | 6 | 768 | 3 min 53 s |
| lower | 10 | 6 | 768 | 2 min 51 s |
| fewer pictures | 14 | 3 (first cut's keyframe + both faces) | 768 | 1 min 48 s |
| smaller pictures | 14 | 6 | 512 | 3 min 54 s |
| middle | 25 | 6 | 768 | 6 min 52 s |

Time is wall time from job creation to completion; the route's `completed_at` minus
`created_at` agrees within a second. Each of the six references was a JPEG shrunk to 768 on
its short edge (773 KB in all); at 512 they came to 409 KB.

## What the frames show

A contact sheet of each variant at 0.5, 2, 4 and 6.5 s, same seed:

- **Steps: default, 14 and 10 are indistinguishable.** The same framing, the same poses,
  the same faces and clothes at every sampled time. Time is linear in the steps (10: 2:51,
  14: 3:53, 25: 6:52, default: 13:41, so the default is about 50), and the extra steps buy
  nothing visible at this size.
- **Reference size does not matter.** 512 and 768 references give the same frames and the
  same time. The cost is per picture, not per pixel.
- **Dropping the per-cut keyframes does.** With only the first cut's keyframe and the two
  faces, cuts 2 to 4 reframe themselves: Lila turns the other way, Ryder's roll-out is seen
  from a different angle, and Ryder's chambray shirt becomes a lighter tee. The faces hold;
  the staging and wardrobe drift. The time saved (1 min 48 s against 3 min 53 s) is not
  worth that on a shot with more than one cut.

## What changed

- **Steps are a setting**: on a run, on a settings patch, on a render by hand, and
  `DRAMA_VIDEO_STEPS` for the client's default. Each take records the count it rendered with.
- **The default is 14.** The route's default stays available with `DRAMA_VIDEO_STEPS=default`
  or a run's `steps` set by hand; a final render of a chosen take can ask for more.
- **References stay as they were** in fast mode: every cut's keyframe and the face of each
  person on screen. Shrinking them further is pointless and dropping keyframes costs
  continuity.

At 14 steps the two-take render of episode 1 (32 takes) would take about 1.5 hours instead
of 8, and a single take about 45 minutes, before any wait in the route's shared queue.
