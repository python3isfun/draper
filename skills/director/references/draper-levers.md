# Where each finding goes in Draper

A director's finding is only useful once it names the step, file and rule that would have
prevented it. The render can't be closer or faster than the storyboard, and the storyboard
can't cut faster than the lines allow, so fix upstream first.

| Finding | Step | Where | What to change |
| --- | --- | --- | --- |
| Long shots, few cuts | Storyboard | `src/drama_agent/pace.py`, `COVERAGE_RULES` and `storyboard_rules()` | A shot is a clip of 4 to 8 s, cut inside by the planner (`setup_s` per pace: brisk 1.5 to 3 s a view; `shots/planner.py`, `PLANNER_MAX_SUB_SHOTS`). If clips still hold one view, look at the plans first: a one-cut fallback (`source: single`) or a planner that kept the storyboard's single framing |
| Two-shots carry the exchange | Storyboard | `pace.COVERAGE_RULES`; `validation/hard_checks.py`, `check_coverage` (at most one shot in two is a two-shot or group shot; consecutive shots differ) | The size defaults and the share of group shots |
| No reaction at the turn | Storyboard and cut plan | `pace.storyboard_rules()`; `pace.planner_rules()` (`reaction_s`, the listener cap: advice to the planner, no longer checked since the off-screen-line rule replaced it) | Plan a close-up of the person each turning line lands on; a review check `"reaction"` |
| Dead air after a line | Cut plan and trim | Episode runs (`shots/episode.py`, `trim` setting) | Trim to the line's end; cut on the word |
| A re-storyboard that changes nothing | Revision gate | `pipeline/revise.py`, `coverage_in_scope`; `validation/hard_checks.py`, `check_coverage` | Asked to re-storyboard, the model copies the old shots unless a check refuses them. The shots a revision targets are held to the coverage rules even when handed back unchanged, and the check also refuses a line cued in a shot its speaker isn't in. After a revision, look at `changed_ids` and the shot count before drawing anything: a published revision with none changed is a copy |
| Lines too long to cut on | Script | `pipeline/steps.py`, `ScriptStep.directive` | Most lines under 8 words, turns 1 to 3; split speeches across a reaction |
| Slow opening, soft ending | Outline and script | `OutlineStep` / `ScriptStep` directives; story templates (`docs/templates.md`) | Open inside the conflict in 3 s; end on a reversal |
| Flat look, no separation | Storyboard (style brief) and sheets | `StoryboardStep` (writes `style_brief`); cast sheet (`CastStep`) | Motivated contrast light, shallow focus on singles, palette per world, signature per lead |
| Look-alike cast | Design | Design board picks; cast sheet `distinguishing_features` | Distinct faces, hair and build for secondary cast |
| Background or face jumps | Storyboard and keyframes | `pipeline/steps.py`, `NEIGHBOUR_CONTINUITY`; the keyframe references in `shots/` | Usually a reference or continuity miss; redraw the shot |
| Peak at full scale | Assembly | `shots/assemble.py` (`PEAK`, `MUSIC_DB`); the run's `music_db` setting | The mix is limited to about -1 dB (`alimiter`). A peak above that is the encoder's overshoot: lower `PEAK` |
| A line doubled, garbled, or in a stranger's voice | Line parts and video prompt | `shots/plans.py` (`shot_lines`, `heard_lines`); `shots/video_prompt.py` (the "Only the lines in `lines` are heard" rule and its check); `shots/assemble.py` (`lay_over`) | H3 voices any line it is given, in a voice of its own, and garbles a line it has no face for. A part cued in a shot its speaker isn't in is spoken in the speaker's shot beside it and laid over the listener's picture at assembly. Look for a prompt that quotes a line the shot doesn't own, and for a whole line spoken only off screen (still voiced by H3) |
| A lead's face changes between shots | Keyframes | `shots/writer.py`, `picture_sentences` | The clip copies its keyframe, so compare keyframes with the face picture first. With a face picture sent, the look picture keeps build and clothes, not its face. Check the look picture itself: one drawn with a different face pulls every keyframe that uses it |
| The fix for a face is a new design picture | Design board and scene reading | `POST .../images/takes` with a `direction`, then `/v1/image-options/<id>/edit` to correct the wording, then `/v1/generated-images/<id>/select`; `shot_reading.carry_unchanged` | Compare head crops of the draws with the face picture before picking. Check the sheet's wording against what is already on screen first (the sheet said "collarless", every keyframe had a collar). Picking a new picture makes every scene that could use it be read again; choices carry over to the new pick, but the prompts and clips of those scenes are written and rendered again, so replace picks before a full render, not after |
| A keyframe check fails only on frame size | Keyframe check | `shots/judge.py`; the take's `check.items` | A close-up drawn as a medium close-up scores 75 and never passes. For a pass about faces or wardrobe, approve a draw whose only miss is framing; for a pass about pace and coverage, don't |
| A prop picked up, put down and picked up again | Storyboard and cut plan | The storyboard's actions; a plan of your own (`POST .../keyframes/<shot_id>/plan` with `sub_shots`) | One pick-up and one put-down a scene unless the story needs more. A plan of your own changes a shot's action without a new revision; its keyframes are withdrawn and drawn again |
| A flash of an insert, or a listener held too long | Trim | `shots/assemble.py` (`trim_points`, `lay_over`) | A clip is cut 0.6 s after its last sound, so a silent second cut inside it can be left half a second long; a listener's clip with no line keeps the storyboard's length |
| Too many locations | Outline | `OutlineStep` directive | Prefer one or two per episode at a brisk pace |

The creative review step (`ReviewStep.rules`) already reports pacing (`"pacing"`). Extend
it rather than adding a parallel review.

## Snippets

The storyboard of an episode, as planned:

```sh
curl -s "http://127.0.0.1:8000/v1/drama-packages/<package_id>" | python3 -c "
import sys, json
p = json.load(sys.stdin)
shots = [s for s in p['shot_list'] if s['episode_id'] == 'ep_01']
print(len(shots), 'shots,', sum(s['duration_ms'] for s in shots) / 1000, 's')
for s in shots:
    print(s['shot_id'], round(s['duration_ms'] / 1000, 1), '|', s['framing'][:60], '|',
          s['camera'][:40], '| lines', len(s['dialogue_cues']))"
```

Words per line:

```sh
curl -s "http://127.0.0.1:8000/v1/drama-packages/<package_id>" | python3 -c "
import sys, json
p = json.load(sys.stdin)
ep = next(e for e in p['episodes'] if e['episode_id'] == 'ep_01')
lines = [b['dialogue'] for s in ep['scenes'] for b in s['beats'] if b.get('dialogue')]
w = [len(l['text'].split()) for l in lines]
print(len(w), 'lines, mean', round(sum(w) / len(w), 1), 'words, over 8:', sum(x > 8 for x in w))"
```

Was the storyboard written under the current coverage rules?

```sh
git log -S"COVERAGE_RULES" --format="%h %ad %s" --date=iso | tail -1   # when the rules arrived
sqlite3 "file:data/drama.db?mode=ro" "select revision_id, created_at from revisions where package_id='<package_id>'"
```

An episode run's settings (cut plan, trim, music):

```sh
sqlite3 "file:data/drama.db?mode=ro" "select json_extract(data,'$.settings') from episode_runs where run_id='<run_id>'"
```
