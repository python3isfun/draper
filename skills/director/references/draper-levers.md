# Where each finding goes in Draper

A director's finding is only useful once it names the step, file and rule that would have
prevented it. The render can't be closer or faster than the storyboard, and the storyboard
can't cut faster than the lines allow, so fix upstream first.

| Finding | Step | Where | What to change |
| --- | --- | --- | --- |
| Long shots, few cuts | Storyboard | `src/drama_agent/pace.py`, `COVERAGE_RULES` and `storyboard_rules()` | Shot length per pace (brisk: 1.5 to 3 s, one line per shot, one hold per scene) |
| Two-shots carry the exchange | Storyboard | `pace.COVERAGE_RULES`; `validation/hard_checks.py`, `check_coverage` (at most one shot in two is a two-shot or group shot; consecutive shots differ) | The size defaults and the share of group shots |
| No reaction at the turn | Storyboard and cut plan | `pace.storyboard_rules()`; `pace.planner_rules()` (`reaction_s` caps listener cuts) | Plan a close-up of the person each turning line lands on; a review check `"reaction"` |
| Dead air after a line | Cut plan and trim | Episode runs (`shots/episode.py`, `trim` setting) | Trim to the line's end; cut on the word |
| Lines too long to cut on | Script | `pipeline/steps.py`, `ScriptStep.directive` | Most lines under 8 words, turns 1 to 3; split speeches across a reaction |
| Slow opening, soft ending | Outline and script | `OutlineStep` / `ScriptStep` directives; story templates (`docs/templates.md`) | Open inside the conflict in 3 s; end on a reversal |
| Flat look, no separation | Storyboard (style brief) and sheets | `StoryboardStep` (writes `style_brief`); cast sheet (`CastStep`) | Motivated contrast light, shallow focus on singles, palette per world, signature per lead |
| Look-alike cast | Design | Design board picks; cast sheet `distinguishing_features` | Distinct faces, hair and build for secondary cast |
| Background or face jumps | Storyboard and keyframes | `pipeline/steps.py`, `NEIGHBOUR_CONTINUITY`; the keyframe references in `shots/` | Usually a reference or continuity miss; redraw the shot |
| Peak at full scale | Assembly | `shots/assemble.py` (`MUSIC_DB`, the mix); the run's `music_db` setting | There is no limiter yet: add one (ffmpeg `alimiter`, about -1 dB) after the mix, or lower the music bed |
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
