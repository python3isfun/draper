# MiniMax-H3-Image: what one keyframe draw costs, and how to draw faster

26 Nov 2026 (measured 27 Nov 02:26 to 02:31 UTC). Follows
[the size-floor report](h3-image-size-floor.md), which timed draws without references.

## Summary

Keyframes stopped drawing on 25 Nov: three in four draws came back `HTTP 502`. The H3
image server was running out of GPU memory on a draw, dying, and restarting, 60 times in
four days. This report gives the cause, what a draw costs in time and memory, where a
keyframe's time goes, and whether a second image server on the video server's cards would
help.

- **The cause was a full card, not the app.** The image server runs on GPUs 1 and 2 of
  h100-087. Since 24 Nov, three speech-recognition containers that are not ours hold about
  18 GB more of GPU 2. The server is left with about 2.4 GB above its idle use.
- **References are what a draw costs.** Without references a draw takes 1.2 s at any size
  from 512x896 to 1536x2720 and adds no memory. Each reference adds about 1.6 s and
  0.35 GB a card: 6 references take 10 to 14 s and 2 GB.
- **Size matters only above 1024 px.** References at 512, 768 and 1024 px cost the same.
  At 1536 px they cost more memory than the card has left: with 5 or 6 of them, no draw
  in 103 succeeded.
- **9 references do not fit today at any size.** One such draw at 512x896 took the server
  down for seven minutes during this measurement.
- **The GPU is about 40% of a keyframe round.** The rest is the text model writing the
  prompt and the vision model checking the draws.
- **A second image server fits on the video server's cards, a third does not.** One server
  needs two cards. It costs all video rendering while it is there.

Changed with this report: keyframes are drawn at 1024x1824 (a setting), from at most six
references (a new default), and the run draws two shots at once. Since the size change, 68
draws have finished and none has failed.

## What happened

| When (UTC) | What |
| --- | --- |
| 11 Nov | The image server moves to Kubernetes: one pod, `minimax-h3-still`, on GPUs 1 and 2, sharing each card with one `asr-distil` worker (7.7 GB) |
| 20 to 23 Nov | 395 keyframe draws finish, 1 fails |
| 24 Nov 06:39 | `civic-asr-distil-gpu2` starts on GPU 2 (a Docker container outside Kubernetes) |
| 24 Nov 19:08 | `civic-asr-qwen-test-gpu2` starts on GPU 2: vLLM serving Qwen3-ASR, 11.3 GB |
| 25 Nov | 22 draws, all fail with `HTTP 502: The model server could not generate the image` |
| 26 Nov | 60 draws finish, 168 fail |
| 26 Nov 13:10 | `civic-asr-qwen-gateway` starts on GPU 2 |
| 27 Nov 02:10 | Keyframes drawn at 1024x1824: every draw since has finished |

One failed draw, from the server's log (27 Nov 01:51): the request is 1536x2720 with
reference pictures; on the first denoising step the second card has 0.76 GB free and
PyTorch asks for 1 GB.

```
CUDA out of memory. Tried to allocate 1022.00 MiB. GPU 1 has a total capacity of
79.18 GiB of which 758.31 MiB is free. Process ... has 7.52 GiB ... 3.94 GiB ...
11.08 GiB ... 2.50 GiB ... 518.00 MiB ... 52.86 GiB memory in use.
Worker 1 cannot safely continue: Multi-rank forward ran out of memory; restart the server.
```

The server does not recover on its own: its worker process exits, `/health` answers 503,
and every request fails until the liveness probe has failed ten times, 30 s apart. Then
the pod restarts and loads for about 90 s. So one draw that does not fit costs everyone
about seven minutes, and the app's retry arrived as the server came back and took it down
again.

GPU 2 at rest, 27 Nov 02:26 (81,559 MiB):

| On the card | Memory |
| --- | --- |
| H3 image server, one of its two ranks | 52.1 GB |
| `asr-distil-gpu2` (there since October) | 7.7 GB |
| `civic-asr-qwen-test-gpu2` | 11.3 GB |
| `civic-asr-distil-gpu2` and `civic-asr-qwen-gateway` | 4.0 GB and 2.5 GB (which is which was not checked) |
| A small process on both cards | 0.5 GB |
| Free | 3.3 GB by `nvidia-smi`; about 2.4 GB usable by the server |

The runbook (`gpu-infra/docs/h100-087-minimax-h3.md`) sizes the card for the server and
one `asr-distil` worker: about 60 GB of 81.5. Nothing in Kubernetes stops a Docker
container from taking a card that the `nvidia.com/gpu-pair-g12` resource already gives to
the image server.

## Method

- **Server:** the production pod, unchanged: SGLang `6be0d75ae0`, MiniMax-H3-Image, two
  cards (`--tp-size=2`, `--component-residency all=resident`), six denoising steps.
- **Route:** straight to the Service on the host (`http://10.43.236.96:8000`,
  `/v1/images/generations` and `/v1/images/edits`), not through MCPHub, so the times have
  no network or gateway in them. The episode run was paused: nothing else was drawing.
- **Cases:** four 9:16 sizes (512x896, 768x1344, 1024x1824, 1536x2720) with 0, 1, 3, 6 and
  9 references, each reference the same picture at the case's reference size. Two draws a
  case, seeds 1000 and 1001. One fixed prompt of about 60 words.
- **Measured:** wall time; the server's own `inference_time_s` and `peak_memory_mb`
  (PyTorch's peak on one rank); and `nvidia-smi` on GPUs 1 and 2 every 100 ms for memory
  and utilisation.
- **Guard:** cases ran from the least to the most pixels in total, to stop before one that
  would not fit. The guard was wrong, see [Limits](#limits).

The script and the raw results are in
[`h3-image-per-draw-cost/`](h3-image-per-draw-cost/): `h3bench.py`, `results.jsonl`.

## What a draw costs

Times are the server's inference time, both draws. Memory is the rise over the idle
server on one card (PyTorch's peak, idle 48.4 GB). GPU use is the mean over the draw.

| References | Output | Reference size | Time | Memory | GPU use |
| --- | --- | --- | --- | --- | --- |
| 0 | 512x896 | | 1.2 to 1.3 s | 0 | 54 to 67% |
| 0 | 768x1344 | | 1.2 s | 0 | 53 to 69% |
| 0 | 1024x1824 | | 1.3 s | 0 | 50 to 64% |
| 0 | 1536x2720 | | 1.1 s | 0 | 60 to 68% |
| 1 | 512x896 | 512x896 | 2.4 to 2.9 s | 0.4 GB | 65 to 78% |
| 1 | 768x1344 | 768x1344 | 2.4 to 2.9 s | 0.4 GB | 66 to 76% |
| 1 | 1024x1824 | 1024x1824 | 2.4 to 3.0 s | 0.2 to 0.4 GB | 66 to 77% |
| 3 | 512x896 | 512x896 | 4.9 to 5.5 s | 0.6 to 1.1 GB | 80 to 85% |
| 3 | 768x1344 | 768x1344 | 5.0 to 5.9 s | 0.6 to 1.1 GB | 78 to 81% |
| 3 | 1024x1824 | 512x896 | 4.9 s | 1.1 GB | 80 to 84% |
| 6 | 512x896 | 512x896 | 10.1 to 11.3 s | 1.9 to 2.2 GB | 86 to 89% |
| 9 | 512x896 | 512x896 | out of memory | more than the card had | |

Three things follow.

**The output size is free.** A 4 MP picture takes the same 1.2 s as a 0.5 MP one, with the
same memory. (The size-floor report found 3.5 s at 4 MP against 1.8 s at 0.8 MP through
MCPHub: that difference is the gateway carrying a larger picture, not the model.)

**A reference costs the same at 512, 768 and 1024 px.** One reference at 512x896 and one at
1024x1824 take the same time and the same memory; so do three at 512x896 and three at
768x1344. Up to 1024 px, only how many there are counts: about 1.6 s and 0.35 GB each.

**References at 1536 px cost more.** They could not be measured here without stopping the
server, but the app's own draws since 25 Nov show it:

| References | At 1536x2720: finished / failed | At 1024x1824: finished / failed |
| --- | --- | --- |
| 3 | 68 / 61 | |
| 4 | 23 / 38 | 2 / 0 |
| 5 | 0 / 55 | 6 / 0 |
| 6 | 0 / 48 | 18 / 0 |

(Counts at 1024x1824 as of 02:31 UTC. When this was written, 68 draws had finished at that
size and none had failed.)

Inside a real keyframe draw (1024x1824, 6 references, a 3,000-character prompt; the
server's log of 02:12 to 02:26 UTC), 11.6 to 14.3 s:

| Stage | Time |
| --- | --- |
| Text encoding | 1.1 to 1.4 s |
| Reference (visual) encoding | 2.0 to 2.5 s |
| Denoising, six steps | 8.2 to 10.0 s |
| Decoding | 0.2 s |

With 4 references the same stages take 0.8, 1.5 and 5.8 s. Both cards run at 80 to 90%
while a draw with references is on them: the draw is GPU-bound, not waiting on anything.

## Where a keyframe's time goes

One keyframe round is: write the prompt, draw twice, check both draws. A cut takes one to
three rounds. One round traced from the run and the server's log (6 references):

| Step | Time |
| --- | --- |
| The text model writes the prompt | 18 s |
| Two draws, one after the other | 29 s |
| The vision model checks the draws | 24 s |
| Total | 71 s |

Rounds since the size change took 69 to 166 s. With one shot at a time, the image server
was busy for about 40% of the run: it has room for a second shot, and about half a third,
before a draw has to wait for another.

## Could the video server's cards take image servers?

The question: scale the Ref2VA video server on GPUs 3 and 4 to zero, and run image
servers there, for three in all.

GPUs 3 and 4, 27 Nov 02:22: 47.7 GB used of 81.5 GB each, idle. Each holds one rank of
the video server (39.4 GB), one `asr-distil` worker (7.7 GB) and a 0.5 GB process. No
`civic-asr` container is on them.

**Two image servers are possible. Three are not.**

- **One server needs two cards.** Each card holds 47.3 GB of weights: half the text
  encoder (24.8 GB), half the transformer (19.9 GB), the VAE and a table. `--tp-size=2`
  and `--encoder-parallel=fold` split both models across the two cards, so a server on one
  card would need about 92 GB of an 81.5 GB card. This is read from the server's load log
  and the flags' own descriptions; a one-card server was not started to check.
- **A second server fits on GPUs 3 and 4.** Without the video server each card has about
  73 GB free. The image server needs 52 to 56 GB with its draws: about 17 GB to spare,
  against 2.4 GB on GPU 2 today. Nine references and 1536 px would fit there.
- **Little has to be built.** A second Deployment that asks for
  `nvidia.com/gpu-pair-g34` and carries the label `app: minimax-h3-still` joins the
  Service the first one is behind. The image API keeps no state between requests, so
  either pod can answer. MCPHub's role names the Service, so nothing changes there. The
  pair resource has a count of one: the pod waits until the video server has let go of the
  cards, and cannot start beside it. The image and flags are the ones GPUs 1 and 2 already
  run next to an `asr-distil` worker.
- **It costs all video.** While the image server is on those cards, no clip renders: not
  the app's, and not drama-studio's two workers on the host, which call the same server. A
  job in flight is lost (the video server keeps job state in its process). Coming back
  means loading 196 GB of video weights, and the video server pins about 117 GB of host
  memory when it runs.
- **The app sends one H3 draw at a time.** `ShotFrameRunner` holds one lock for the GPU.
  A second server is idle until that lock becomes a limit of two.

What it would buy: the ceiling goes from two or three shots at once to about five, and a
take's two draws could run side by side (29 s down to about 15 s of a 71 s round). Per
keyframe that is about 20%; for a batch, it is the higher ceiling that counts.

## What was changed

| Change | Where | Effect |
| --- | --- | --- |
| Keyframes at 1024x1824, references at a 1024 px short edge | `DRAMA_KEYFRAME_SHORT_EDGE=1024` in `.env` (the default stays 1536) | Draws fit the card again |
| At most six references a keyframe | `DRAMA_KEYFRAME_MAX_REFERENCES`, default 6, up to 9 | No draw can take the server down while GPU 2 is full; a draw is 10 to 14 s at most |
| Two shots at once | the run's `parallel` setting | The image server's idle 60% is used |
| Retries of a 502 span nine minutes | `MCPHUB_IMAGE_RETRIES`, default 8 | A draw outlasts one restart of the server |

With six references, the order decides what is left out: the keyframe before, each
person's look, the set, the previous shot's keyframe, the props, then faces. A shot with
many people or props loses its faces first, then its props. A prompt version written for
more than six pictures is refused for H3 with a message that says so.

## Recommendations

1. **Get GPU 2 back.** Ask whoever runs the `civic-asr` containers to move
   `civic-asr-qwen-test-gpu2` (11.3 GB) off the card. That alone restores 1536x2720
   keyframes and nine references. Nothing else here fixes the cause.
2. **Keep fewer references where the picture allows.** Going from six to three halves the
   draw (10 s to 5 s). Which references a keyframe can do without was not tested: it is a
   question of likeness, not of speed.
3. **Raise `parallel` to 3 only with a second server.** At two, the one server is near
   full.
4. **Rotate a second image server onto GPUs 3 and 4 for a long keyframe batch only**, when
   nobody needs video for the length of it, and with the GPU lock in the app made a limit
   of two first.
5. **Make an out-of-memory draw cheaper.** The seven minutes come from the liveness probe
   (ten failures, 30 s apart). Three failures would bring the server back in about three
   minutes. This is a change to the manifest in `gpu-infra`, not made here.

## Limits

- **The measurement stopped the server once.** The guard stopped a series when a case left
  under 700 MiB free on GPU 2. The six-reference case left 1.6 GB on its second draw, so
  the nine-reference case ran, did not fit, and the server was down from 02:31 to 02:38
  UTC (its 61st restart). The run was paused, so no draw of the app's was lost.
- **Nothing above 6 references or with 1536 px references was measured.** Those rows come
  from the app's draw history, which gives finished or failed, not time or memory.
- **Two draws a case, one prompt, one reference picture repeated.** Real keyframes use
  different pictures and prompts ten times longer; their draws ran 15 to 40% slower than
  the six-reference case here.
- **A keyframe's round was traced once.** The text and vision calls of a keyframe are not
  timed in the database; the split comes from one take's timestamps and the server's log.
- **The one-card and two-server figures are estimates.** Neither was started.
