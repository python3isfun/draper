"""Episode runs: one button from the storyboard to the clips and an assembled episode,
with holds, pause and resume."""

import asyncio
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from drama_agent.api import create_app
from drama_agent.fixtures.sample import SAMPLE_SEED
from drama_agent.imagegen import FixtureImageClient
from drama_agent.llm import FixtureModelClient
from drama_agent.media import MediaStore
from drama_agent.models.package import utcnow
from drama_agent.shot_frames import ShotFrameRunner
from drama_agent.shots.clips import ShotClip, clip_prompt, clip_state
from drama_agent.shots.episode import EpisodeRun, EpisodeRunner, RunSettings, episode_shots
from drama_agent.shots.models import PromptBlock, ShotPlan, ShotVideoPrompt, SubShot
from drama_agent.store import InMemoryRepository
from drama_agent.videogen import FixtureVideoClient, ffmpeg_available

from .conftest import wait_job

BODY = {"sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}]}

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")


class Studio:
    """The app with fixture text, image and video clients, and the sample story made."""

    def __init__(self, tmp_path):
        self.text = FixtureModelClient()
        self.images = FixtureImageClient()
        self.video = FixtureVideoClient()
        self.media = MediaStore(tmp_path)
        self.repo = InMemoryRepository()
        self.app = create_app(repo=self.repo, client=self.text, image_client=self.images,
                              media=self.media, poll_s=0.01, video_client=self.video,
                              run_poll_s=0.05)  # fmt: skip

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        job = wait_job(self.http, self.http.post("/v1/drama-jobs", json=BODY).json()["job_id"])
        self.pid = job["package_id"]
        self.pkg = self.repo.get_package(self.pid)
        self.episode = self.pkg.episodes[0].episode_id
        self.base = f"/v1/drama-packages/{self.pid}/episodes/{self.episode}/run"
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def start(self, **settings) -> dict:
        r = self.http.post(self.base, json=settings)
        assert r.status_code == 202, r.text
        return r.json()

    def run(self) -> dict:
        return self.http.get(self.base).json()

    def wait(self, until, timeout: float = 120.0, what: str = "") -> dict:
        deadline = time.monotonic() + timeout
        while not until(run := self.run()):
            assert time.monotonic() < deadline, f"{what or 'condition'} not reached: " + json.dumps(
                {"status": run["status"], "holds": run["holds"], "counts": run["counts"],
                 "stages": [(s["shot_id"], s["stage"]) for s in run["shots"]],
                 "events": [e["text"] for e in run["events"][-8:]]}, indent=1)  # fmt: skip
            time.sleep(0.05)
        return run


def done(run: dict) -> bool:
    assert run["status"] != "failed", run["error"]
    return run["status"] == "done"


@needs_ffmpeg
def test_generate_episode_takes_every_shot_to_a_clip_and_cuts_the_episode(tmp_path):
    with Studio(tmp_path) as s:
        first = s.start()
        assert first["status"] == "running" and first["shots"]
        n = len(episode_shots(s.pkg, s.episode))
        run = s.wait(done, what="the run finishing")
        counts = run["counts"]
        assert counts["shots"] == n and counts["cuts_approved"] == counts["cuts"] > 0
        assert counts["prompts_current"] == n and counts["clips_done"] == n
        assert all(r["stage"] == "done" for r in run["shots"])
        assert run["holds"] == {}
        # The design pictures were drawn first, by the run itself.
        assert any("design pictures" in e["text"] for e in run["events"])
        # Every clip went to the video client with its keyframes first, as references.
        assert len(s.video.calls) == n
        for call in s.video.calls:
            assert 4 <= call["duration_s"] <= 15 and call["ratio"] == "9:16"
            assert call["references"] and "<Picture 1>" in call["prompt"]
            assert "[Shot 1]" in call["prompt"]
        # The assembled episode plays as one file, as long as its clips together.
        out = run["output"]
        assert out and out["clips"] == n
        total = sum(c.duration_s for c in s.repo.list_shot_clips(s.pid, s.pkg.revision_id))
        assert abs(out["duration_s"] - total) < 1.5
        r = s.http.get(f"/v1/episode-runs/{run['run_id']}/output")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert s.http.get(f"/v1/episode-runs/{run['run_id']}/poster").status_code == 200
        # The board shows each shot's clip as current.
        board = s.http.get(f"/v1/drama-packages/{s.pid}/keyframes").json()
        mine = [x for x in board["shots"] if x["episode_id"] == s.episode]
        assert all(x["clip"]["state"] == "current" for x in mine)
        clips = s.http.get(f"/v1/drama-packages/{s.pid}/clips").json()
        shot = next(x for x in clips["shots"] if x["episode_id"] == s.episode)
        clip_id = shot["newest"]["clip_id"]
        assert s.http.get(f"/v1/shot-clips/{clip_id}/content").status_code == 200
        assert s.http.get(f"/v1/shot-clips/{clip_id}/poster").status_code == 200
        # The episodes list carries the run.
        eps = s.http.get(f"/v1/drama-packages/{s.pid}/episodes").json()
        assert eps[0]["run"]["status"] == "done" and eps[0]["shots"] == n
        # A second run finds everything current and finishes at once, re-cutting nothing.
        again = s.start()
        again = s.wait(done, what="the second run")
        assert again["output"]["print"] == out["print"] and len(s.video.calls) == n


def test_the_fast_option_lowers_the_resolution_and_sends_the_anchors_only(tmp_path):
    with Studio(tmp_path) as s:
        s.start(fast=True)
        run = s.wait(done, what="the fast run finishing")
        assert run["settings"]["fast"] is True
        n = len(episode_shots(s.pkg, s.episode))
        assert run["counts"]["clips_done"] == n and len(s.video.calls) == n
        # Every clip went at the fast short edge, within the usual picture cap.
        for call in s.video.calls:
            assert call["short_edge"] == 512
            assert 1 <= len(call["references"]) <= 9
        # The clips record that they were rendered fast, and sent only the identity
        # anchors: every cut's keyframe, then each person's face.
        clips = s.repo.list_shot_clips(s.pid, s.pkg.revision_id)
        assert clips and all(c.fast for c in clips)
        cuts = {r["shot_id"]: r["cuts"] for r in run["shots"]}
        for c in clips:
            kinds = [r.kind for r in c.references]
            assert set(kinds) <= {"keyframe", "face"} and kinds.count("keyframe") == cuts[c.shot_id]
            assert kinds == sorted(kinds, key=lambda k: k != "keyframe")  # keyframes first
        assert any("face" in {r.kind for r in c.references} for c in clips)
        # Each clip records its render statistics: times, size and the probed picture.
        for c in clips:
            assert c.started_at and c.finished_at and c.created_at <= c.started_at <= c.finished_at
            assert c.render_s is not None and c.render_s >= 0 and c.queue_s is not None
            assert c.bytes and c.bytes > 0 and c.width and c.height and c.media_s
        view = next(x for x in run["shots"] if x["clip"]["clip_id"] == clips[0].clip_id)["clip"]
        assert (
            view["bytes"] == clips[0].bytes
            and view["width"] == clips[0].width
            and "render_s" in view
        )


@needs_ffmpeg
def test_more_takes_render_beside_the_clip_and_an_edited_prompt_outdates_them(tmp_path):
    with Studio(tmp_path) as s:
        s.start()
        s.wait(done, what="the run finishing")
        row = s.run()["shots"][0]
        sid, first = row["shot_id"], row["clip"]["clip_id"]
        assert [c["clip_id"] for c in row["clips"]] == [first] and row["clip"]["seed"] == 1
        # Two more takes queue with seeds the shot hasn't used; each shows beside the clip.
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{sid}", json={"takes": 2})
        assert r.status_code == 202, r.text
        assert [c["seed"] for c in r.json()] == [2, 3]
        row = next(x for x in s.run()["shots"] if x["shot_id"] == sid)
        assert len(row["clips"]) == 3 and row["clips"][-1]["clip_id"] == first
        assert all(c["state"] in ("rendering", "current") for c in row["clips"])
        assert "ahead" in row["clips"][0]
        # A fifth take at once is refused while four are in flight; the seed can be chosen.
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{sid}", json={"takes": 1, "seed": 40})
        assert r.status_code == 202 and r.json()[0]["seed"] == 40
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{sid}", json={"takes": 2})
        assert r.status_code == 409
        s.wait(lambda r: all(c["state"] == "current" for x in r["shots"] if x["shot_id"] == sid
                             for c in x["clips"]), what="the takes finishing")  # fmt: skip
        assert {c["seed"] for c in s.video.calls[-3:]} == {2, 3, 40}
        # The newest take is the shot's clip; using an older one makes it the newest.
        row = next(x for x in s.run()["shots"] if x["shot_id"] == sid)
        assert row["clip"]["seed"] == 40
        r = s.http.post(f"/v1/shot-clips/{first}/use")
        assert r.status_code == 200
        row = next(x for x in s.run()["shots"] if x["shot_id"] == sid)
        assert row["clip"]["clip_id"] == first and row["clips"][0]["clip_id"] == first
        # Editing the prompt by hand keeps it current and marks every take out of date.
        vp = row["prompt"]
        assert vp["state"] == "current" and vp["blocks"] and vp["pictures"]
        blocks = [{**b, "text": b["text"] + " A moth circles the lamp."} for b in vp["blocks"]]
        url = f"/v1/drama-packages/{s.pid}/keyframes/{sid}/video-prompt"
        bad = [{**blocks[0], "text": "{pic:nope} stands still."}, *blocks[1:]]
        assert s.http.put(url, json={"blocks": bad, "sound": vp["sound"]}).status_code == 422
        r = s.http.put(url, json={"blocks": blocks, "sound": "Rain on the window."})
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "user" and r.json()["sound"] == "Rain on the window."
        row = next(x for x in s.run()["shots"] if x["shot_id"] == sid)
        assert row["prompt"]["state"] == "current" and row["prompt"]["source"] == "user"
        assert row["prompt"]["blocks"][0]["text"].endswith("A moth circles the lamp.")
        assert all(c["state"] == "outdated" for c in row["clips"])
        # Rendered again, the clip carries the edit.
        calls = len(s.video.calls)
        assert s.http.post(f"/v1/drama-packages/{s.pid}/clips/{sid}", json={}).status_code == 202
        s.wait(lambda r: len(s.video.calls) > calls and next(
            x for x in r["shots"] if x["shot_id"] == sid)["clip"]["state"] == "current",
            what="the edited clip")  # fmt: skip
        assert "A moth circles the lamp." in s.video.calls[-1]["prompt"]
        assert s.video.calls[-1]["prompt"].endswith("Rain on the window.")
        # A prompt whose writer failed is fixed by an edit: it is done again and renders.
        stored = s.repo.get_video_prompt(s.pid, s.pkg.revision_id, sid)
        stored.status, stored.error = "failed", "the writer timed out"
        s.repo.save_video_prompt(stored)
        r = s.http.put(url, json={"blocks": blocks, "sound": "Rain on the window."})
        assert r.status_code == 200 and r.json()["status"] == "done" and not r.json()["error"]
        assert s.http.post(f"/v1/drama-packages/{s.pid}/clips/{sid}", json={}).status_code == 202


@needs_ffmpeg
def test_a_run_renders_as_many_takes_of_each_shot_as_asked(tmp_path):
    with Studio(tmp_path) as s:
        s.start(takes=2)
        run = s.wait(done, what="the two-take run finishing")
        n = len(episode_shots(s.pkg, s.episode))
        c = run["counts"]
        assert c["takes"] == 2 and c["takes_total"] == 2 * n and c["takes_done"] == 2 * n
        assert c["clips_done"] == n and len(s.video.calls) == 2 * n
        assert all(r["stage"] == "done" for r in run["shots"])
        # Every shot has two current takes with their own seeds, the newest in the cut.
        for row in run["shots"]:
            seeds = [t["seed"] for t in row["clips"]]
            assert len(seeds) == 2 and len(set(seeds)) == 2
            assert all(t["state"] == "current" for t in row["clips"])
            assert row["clip"]["clip_id"] == row["clips"][0]["clip_id"]
        assert any("take 2 of 2" in e["text"] for e in run["events"])
        assert run["output"]["clips"] == n
        # Asking for a third take on the way resumes the run at the clips.
        r = s.http.patch(f"/v1/episode-runs/{run['run_id']}/settings", json={"takes": 3})
        assert r.status_code == 200
        again = s.start(takes=3)
        again = s.wait(done, what="the three-take run")
        assert again["counts"]["takes_done"] == 3 * n and len(s.video.calls) == 3 * n


@needs_ffmpeg
def test_holding_every_approval_waits_for_a_person_and_a_release_goes_on(tmp_path):
    with Studio(tmp_path) as s:
        s.start(holds=["approve", "render"], render=True)
        run = s.wait(lambda r: any(k.endswith(":cut1") for k in r["holds"]), what="the first hold")
        assert run["status"] == "running"
        held = [x for x in run["shots"] if x["holds"]]
        assert held and held[0]["stage"] == "waiting"
        assert "waits for your approval" in next(iter(held[0]["holds"].values()))
        # Nothing was approved by the run.
        assert run["counts"]["cuts_approved"] == 0
        # A person approves one shot's cut by hand: the run goes on with that shot.
        shot = held[0]
        kf = shot["keyframes"][0]
        board = s.http.get(f"/v1/drama-packages/{s.pid}/keyframes").json()
        takes = next(x for x in board["shots"] if x["shot_id"] == shot["shot_id"])["sub_shots"][0][
            "takes"
        ]
        r = s.http.post(f"/v1/shot-takes/{takes[-1]['take_id']}/approve", json={"n": 0})
        assert r.status_code == 200 and kf["approved"] is False
        # Release every approval: the run approves the rest itself, then holds before rendering.
        r = s.http.post(f"/v1/episode-runs/{run['run_id']}/release", json={"key": "approve"})
        assert r.status_code == 200 and "approve" not in r.json()["settings"]["holds"]
        # The run holds before the first clip while the other shots' keyframes go on.
        run = s.wait(lambda r: "render" in r["holds"]
                     and r["counts"]["prompts_current"] == r["counts"]["shots"],
                     what="the render hold with every prompt written")  # fmt: skip
        assert run["counts"]["cuts_approved"] == run["counts"]["cuts"]
        assert not s.video.calls
        s.http.post(f"/v1/episode-runs/{run['run_id']}/release", json={"key": "render"})
        run = s.wait(done, what="the run finishing after the release")
        assert len(s.video.calls) == run["counts"]["shots"]


@needs_ffmpeg
def test_pause_stops_new_work_and_resume_goes_on_from_the_board(tmp_path):
    with Studio(tmp_path) as s:
        run = s.start()
        r = s.http.post(f"/v1/episode-runs/{run['run_id']}/pause")
        assert r.status_code == 202 and r.json()["status"] == "paused"
        # What was running finishes; nothing new starts.
        deadline = time.monotonic() + 30
        while s.http.get(f"/v1/drama-packages/{s.pid}/keyframes").json()["busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        before = s.run()["counts"]
        time.sleep(0.3)
        assert s.run()["counts"] == before and s.run()["status"] == "paused"
        assert s.http.post(f"/v1/episode-runs/{run['run_id']}/pause").status_code == 409
        r = s.http.post(f"/v1/episode-runs/{run['run_id']}/resume")
        assert r.status_code == 202 and r.json()["status"] == "running"
        run = s.wait(done, what="the run finishing after resume")
        assert run["counts"]["clips_done"] == run["counts"]["shots"]


@needs_ffmpeg
def test_skipped_shots_are_left_out_and_one_run_per_episode(tmp_path):
    with Studio(tmp_path) as s:
        shots = episode_shots(s.pkg, s.episode)
        skip = shots[-1].shot_id
        run = s.start(skip_shots=[skip], assemble=False)
        assert s.http.post(s.base, json={}).status_code == 409  # one at a time
        run = s.wait(done, what="the run finishing")
        row = next(x for x in run["shots"] if x["shot_id"] == skip)
        assert row["stage"] == "skipped" and run["counts"]["shots"] == len(shots) - 1
        assert run["output"] is None and len(s.video.calls) == len(shots) - 1
        # Settings can change as it goes; a finished run is done.
        r = s.http.patch(f"/v1/episode-runs/{run['run_id']}/settings", json={"draws": 1})
        assert r.status_code == 200 and r.json()["settings"]["draws"] == 1


@needs_ffmpeg
def test_a_take_that_puts_a_line_on_the_wrong_face_is_rejected_and_rendered_again(tmp_path):
    """The speech check looks at every rendered take; one that has the wrong person
    talking is rejected, another take rendered, and the episode cut from the good one."""
    with Studio(tmp_path) as s:
        seen: list[int] = []

        def wrong_face(out, attempt):
            seen.append(attempt)
            if len(seen) == 1:  # the first take checked: someone else is talking
                out.cuts[0].speaking = ["someone else"]
            return out

        s.text.faults["speech_check"] = wrong_face
        s.start(render=True, assemble=False)
        run = s.wait(done, what="the run finishing")
        assert len(seen) == run["counts"]["shots"] + 1  # every take checked, one of them twice
        assert len(s.video.calls) == run["counts"]["shots"] + 1
        bad = next(x for x in run["shots"] if len(x["clips"]) == 2)
        newest, first = bad["clips"]
        assert first["state"] == "rejected" and first["check"]["passed"] is False
        assert first["has_file"] and first["status"] == "done"
        finding = next(
            i for i in first["check"]["items"] if not i["passed"] and i["severity"] == "major"
        )
        assert finding["finding"].startswith("someone else is seen speaking")
        assert newest["state"] == "current" and newest["check"]["passed"] is True
        assert newest["seed"] != first["seed"]
        assert bad["clip"]["clip_id"] == newest["clip_id"]  # the episode is cut from the good one
        assert any("failed the speech check" in e["text"] for e in run["events"])
        # Every other take passed and says so.
        others = [c for x in run["shots"] for c in x["clips"] if c["clip_id"] != first["clip_id"]]
        assert all(c["check"]["passed"] and c["check"]["frames"] > 0 for c in others)
        # A person may accept the rejected take anyway: it is then the newest and current.
        r = s.http.post(f"/v1/shot-clips/{first['clip_id']}/use", json={})
        assert r.status_code == 200 and r.json()["accepted"] is True
        row = next(x for x in s.run()["shots"] if x["shot_id"] == bad["shot_id"])
        assert row["clip"]["clip_id"] == first["clip_id"] and row["clip"]["state"] == "current"
        # Off, nothing is checked.
        before = len(seen)
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{bad['shot_id']}", json={"check": False})
        assert r.status_code == 202, r.text
        s.wait(lambda run: all(not c["status"] in ("pending", "rendering")
                               for x in run["shots"] for c in x["clips"]), what="the render")  # fmt: skip
        assert len(seen) == before
        row = next(x for x in s.run()["shots"] if x["shot_id"] == bad["shot_id"])
        assert row["clip"]["check"] is None and row["clip"]["state"] == "current"


def test_a_run_holds_a_shot_whose_plan_puts_a_line_on_the_wrong_face(tmp_path):
    """A plan the planner couldn't clean (a listener held through someone else's line)
    waits for a person instead of being drawn and rendered through."""
    from drama_agent.shots.plans import shot_lines

    with Studio(tmp_path) as s:
        shot = next(x for x in s.pkg.shot_list if len(x.character_ids) >= 2
                    and shot_lines(s.pkg, x))  # fmt: skip
        [line, *_] = shot_lines(s.pkg, shot)
        listener = next(c for c in shot.character_ids if c != line["speaker_id"])
        clip = -(-shot.duration_ms // 1000) * 1000
        plan = ShotPlan(package_id=s.pid, revision_id=s.pkg.revision_id, shot_id=shot.shot_id,
                        source="user", sub_shots=[
                            SubShot(index=1, start_ms=0, end_ms=clip, framing="close-up",
                                    action="listens", people=[listener],
                                    placement={listener: "left"},
                                    off_screen={line["speaker_id"]: "right"})])  # fmt: skip
        s.repo.save_shot_plan(plan)
        s.start(render=False, assemble=False)
        run = s.wait(lambda r: f"{shot.shot_id}:plan" in r["holds"], what="the plan hold")
        why = run["holds"][f"{shot.shot_id}:plan"]
        assert why.startswith("the cut plan isn't a clean spec for video: an off-screen line")
        held = next(x for x in run["shots"] if x["shot_id"] == shot.shot_id)
        assert held["stage"] == "waiting"
        assert all(k["take_id"] is None for k in held["keyframes"])  # nothing drawn
        # A person fixes the plan (the speaker in frame): the run goes on with the shot.
        r = s.http.post(f"/v1/drama-packages/{s.pid}/keyframes/{shot.shot_id}/plan", json={
            "sub_shots": [{"start_ms": 0, "framing": "two-shot", "action": "talks",
                           "people": shot.character_ids,
                           "placement": {c: ["left", "right", "center"][i % 3]
                                         for i, c in enumerate(shot.character_ids)}}]})  # fmt: skip
        assert r.status_code == 202, r.text
        run = s.wait(done, what="the run finishing after the plan was fixed")
        assert not run["holds"]


def test_a_clip_prompt_numbers_the_pictures_sent_and_drops_the_rest(package):
    shot = package.shot_list[0]
    half = shot.duration_ms // 2
    plan = ShotPlan(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, source="user", sub_shots=[
                        SubShot(index=1, start_ms=0, end_ms=half, framing="wide", action="a"),
                        SubShot(index=2, start_ms=half, end_ms=shot.duration_ms,
                                framing="close", action="b")])  # fmt: skip
    vp = ShotVideoPrompt(package_id=package.package_id, revision_id=package.revision_id,
                         shot_id=shot.shot_id, status="done", sound="Only the rain.",
                         blocks=[
                             PromptBlock(sub_shot=1, text="In the room from {pic:img_set}, the woman "
                                         "from {pic:frame:t1:0} waits. Her coat from {pic:img_look} "
                                         "is wet. The woman from {pic:frame:t1:0} and {pic:img_look} "
                                         "frowns. The woman from {pic:img_look} says in English: "
                                         "\"Hi.\"", camera="static shot"),
                             PromptBlock(sub_shot=2, text="Close on her face from {pic:frame:t2:0}.",
                                         camera="push in with small amplitude at slow speed")])  # fmt: skip
    text = clip_prompt(vp, plan, ["frame:t1:0", "frame:t2:0", "img_set"])
    assert text.startswith(
        "[Shot 1] In the room from <Picture 3>, the woman from <Picture 1> waits."
    )
    assert "img_look" not in text and "Her coat" not in text  # not sent: its sentence goes
    # A sentence that also cites a picture sent is kept, minus the unsent citation.
    assert "The woman from <Picture 1> frowns." in text
    # The spoken line survives even though the speaker's picture is not sent; its citation
    # is stripped, so the clip keeps its voice.
    assert 'The woman says in English: "Hi." The camera holds a static shot.' in text
    assert f"[Shot 2] At {plan.sub_shots[1].timecode}, Close on her face from <Picture 2>." in text
    assert "Camera motion: push in with small amplitude at slow speed." in text
    assert text.endswith("Only the rain.")

    # Keyframes only (the fast option): the descriptive lines about unsent pictures go, but
    # the spoken line stays, with no picture citation left in it.
    fast = clip_prompt(vp, plan, ["frame:t1:0", "frame:t2:0"])
    assert 'The woman says in English: "Hi."' in fast
    assert "{pic:" not in fast and "img_look" not in fast and "img_set" not in fast
    assert "The woman from <Picture 1> frowns." in fast
    assert "In the room, the woman from <Picture 1> waits." in fast  # the set citation dropped


@needs_ffmpeg
def test_a_reference_picture_is_shrunk_to_the_short_edge_asked(tmp_path):
    from drama_agent.shots import assemble as asm

    big = tmp_path / "big.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1536x2720",
                    "-frames:v", "1", str(big)], check=True)  # fmt: skip
    data = asm.shrink_picture(big, 768)
    assert data and data[:3] == b"\xff\xd8\xff" and len(data) < big.stat().st_size
    small = tmp_path / "small.jpg"
    small.write_bytes(data)
    info = asm.probe(small)
    assert (info["width"], info["height"]) == (768, 1360)
    # Already small enough: sent as it is.
    assert asm.shrink_picture(small, 768) is None


@needs_ffmpeg
def test_the_steps_setting_reaches_the_video_model_and_the_take(tmp_path):
    with Studio(tmp_path) as s:
        s.start(render=True, assemble=False, steps=14)
        run = s.wait(done, what="the run finishing")
        assert run["settings"]["steps"] == 14
        assert all(c["steps"] == 14 for c in s.video.calls)
        assert all(c["steps"] == 14 for x in run["shots"] for c in x["clips"])
        # A render by hand says its own; left out, the model's default (None).
        shot = run["shots"][0]["shot_id"]
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{shot}", json={"steps": 8})
        assert r.status_code == 202 and r.json()[0]["steps"] == 8
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{shot}", json={"steps": 101})
        assert r.status_code == 422
        s.wait(lambda run: all(c["status"] not in ("pending", "rendering")
                               for x in run["shots"] for c in x["clips"]), what="the render")  # fmt: skip
        assert s.video.calls[-1]["steps"] == 8
        r = s.http.post(f"/v1/drama-packages/{s.pid}/clips/{shot}", json={})
        assert r.status_code == 202 and r.json()[0]["steps"] is None


def test_the_estimate_survives_a_take_made_the_newest(package):
    """ "Use" moves a take's created_at forward; the estimate measures it from its start."""
    from datetime import timedelta

    from drama_agent.shots.episode import estimate

    shot = package.shot_list[0]
    t0 = utcnow()
    used = ShotClip(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=shot.shot_id, status="done", created_at=t0 + timedelta(hours=1),
                    started_at=t0, finished_at=t0 + timedelta(minutes=5))  # fmt: skip
    run = EpisodeRun(package_id=package.package_id, revision_id=package.revision_id,
                     episode_id=package.episodes[0].episode_id, created_at=t0)  # fmt: skip
    out = estimate(run, [shot], [], [used], 0, 3)
    assert out["per_clip_s"] == 300 and out["remaining_s"] == 900


def test_a_clip_is_current_only_for_the_print_it_was_rendered_from():
    clip = ShotClip(package_id="p", revision_id="r", shot_id="s", status="done", written_from="abc")
    assert clip_state(clip, "abc") == "current"
    assert clip_state(clip, "xyz") == "outdated"
    assert clip_state(clip, None) == "outdated"
    assert clip_state(None, "abc") == "none"
    assert clip_state(clip.model_copy(update={"status": "rendering"}), "abc") == "rendering"
    assert clip_state(clip.model_copy(update={"status": "failed"}), "abc") == "failed"


def test_recover_pauses_running_runs_and_fails_clips_in_flight(tmp_path, package):
    repo = InMemoryRepository()
    repo.save_package(package, True)
    run = EpisodeRun(package_id=package.package_id, revision_id=package.revision_id,
                     episode_id=package.episodes[0].episode_id)  # fmt: skip
    repo.save_episode_run(run)
    clip = ShotClip(package_id=package.package_id, revision_id=package.revision_id,
                    shot_id=package.shot_list[0].shot_id, status="rendering")  # fmt: skip
    repo.save_shot_clip(clip)

    async def go():
        frames = ShotFrameRunner(
            repo, MediaStore(tmp_path), FixtureImageClient(), FixtureModelClient()
        )
        return EpisodeRunner(repo, MediaStore(tmp_path), frames, FixtureVideoClient()).recover()

    assert asyncio.run(go()) == 2
    assert repo.get_episode_run(run.run_id).status == "paused"
    assert repo.get_shot_clip(clip.clip_id).status == "failed"
    assert RunSettings().holds == [] and RunSettings().render
