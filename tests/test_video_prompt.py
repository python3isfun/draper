"""The shot's H3 video prompt: written from the plan, keyframes and pictures once every cut
is approved, checked by rule, and exported (spec_version 3) while it is current."""

import json

from drama_agent.shots.models import PromptBlock
from drama_agent.shots.video_prompt import (
    VideoPromptOutput,
    dialogue_pace,
    prompt_findings,
)

from .test_shot_frames import Board, member


def _approved_two_cuts(b):
    b.design()
    shot = b.busiest()
    sid = shot["shot_id"]
    assert b.http.post(f"{b.base}/{sid}/plan", json=b.two_cuts(shot)).status_code == 202
    b.http.post(f"{b.base}/{sid}", json={"draws": 1})
    [one] = b.wait() and b.takes(sid)
    b.http.post(f"/v1/shot-takes/{one['take_id']}/approve", json={"n": 0})  # starts cut 2
    [two] = b.wait() and b.takes(sid, 2)
    return sid, one, two


def _specs(b) -> dict:
    return json.loads(member(b.zip(), "03_storyboard/shot_specs.json"))


def test_the_prompt_is_written_once_every_cut_is_approved_and_exported(tmp_path):
    with Board(tmp_path) as b:
        sid, one, two = _approved_two_cuts(b)
        assert b.shot(sid)["video_prompt"]["state"] == "none"  # cut 2 isn't approved yet
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        b.wait()
        vp = b.shot(sid)["video_prompt"]
        assert vp["state"] == "current" and vp["problems"] == []
        assert [x["sub_shot"] for x in vp["blocks"]] == [1, 2]
        call = b.calls("video_prompt")[-1]
        assert "says in English" in call.instructions
        assert [c["index"] for c in call.context["cuts"]] == [1, 2]
        # The writer sees each cut's keyframe, and what its check found against the plan.
        assert [m.label for m in call.media] == [c["keyframe"] for c in call.context["cuts"]]
        assert all(isinstance(c["keyframe_check"], list) for c in call.context["cuts"])
        # Every picture sent is named, the keyframes by their label.
        assert f"frame:{one['take_id']}:0" in vp["pictures"]
        text = " ".join(x["text"] for x in vp["blocks"])
        assert all(f"{{pic:{ref}}}" in text for ref in vp["pictures"])

        spec = _specs(b)
        assert spec["spec_version"] == 3
        mine = next(s for s in spec["shots"] if s["shot_id"] == sid)
        prompt = mine["prompt"]
        assert prompt["format"] == "h3-paragraph/1" and prompt["sound"]
        files = {r["file"].rsplit("/", 1)[-1] for r in mine["references"]}
        named = {m for x in prompt["blocks"] for m in _pics(x["text"])}
        assert named and named <= files  # by file name, all of them sent
        assert all(r["named"] for r in mine["references"])
        assert mine["references"][0]["kind"] == "keyframe"
        assert spec["readiness"]["shots_with_prompt"] >= 1

        # A new keyframe for cut 2: the prompt is out of date and left out of the export.
        b.http.post(f"{b.base}/{sid}", json={"sub_shot": 2, "draws": 1})
        b.wait()
        again = b.takes(sid, 2)[-1]
        b.http.post(f"/v1/shot-takes/{again['take_id']}/approve", json={"n": 0})
        assert b.shot(sid)["video_prompt"]["state"] in ("writing", "current")
        b.wait()  # written again on its own
        assert b.shot(sid)["video_prompt"]["state"] == "current"
        assert len(b.calls("video_prompt")) >= 2


def _pics(text: str) -> list[str]:
    import re

    return re.findall(r"\{pic:([^{}\s]+)\}", text)


def test_an_out_of_date_prompt_isnt_exported(tmp_path):
    with Board(tmp_path) as b:
        sid, _, two = _approved_two_cuts(b)
        b.http.post(f"/v1/shot-takes/{two['take_id']}/approve", json={"n": 0})
        b.wait()
        b.http.delete(f"/v1/shot-takes/{two['take_id']}/approve")  # cut 2's keyframe goes
        assert b.shot(sid)["video_prompt"]["state"] == "outdated"
        mine = next(s for s in _specs(b)["shots"] if s["shot_id"] == sid)
        assert mine["prompt"] is None
        assert any("video prompt is out of date" in x for x in mine["not_ready"])


def test_writing_by_hand_and_for_every_shot(tmp_path):
    with Board(tmp_path) as b:
        b.design()
        sid = b.busiest()["shot_id"]
        r = b.http.post(f"{b.base}/{sid}/video-prompt", json={})
        assert r.status_code == 202 and r.json()["status"] == "writing"
        b.wait()
        assert b.shot(sid)["video_prompt"]["state"] == "current"
        r = b.http.post(f"{b.base}/video-prompts", json={})
        assert r.status_code == 202 and sid not in r.json()["started"]  # it is current
        b.wait()
        shots = b.board()["shots"]
        assert all(s["video_prompt"]["state"] == "current" for s in shots)
        r = b.http.post(f"{b.base}/video-prompts", json={"shot_ids": ["nope"]})
        assert r.status_code == 404


def _context(lines=(), pictures=("A", "B")):
    return {
        "pictures": [{"pic": f"{{pic:{p}}}", "kind": "face", "shows": "", "person": "Lila",
                      "cuts": []} for p in pictures],
        "cuts": [{"index": 1, "people": [{"name": "Lila"}],
                  "lines": [{"speaker": "Lila", "line": x, "where": "on screen"} for x in lines]}],
    }  # fmt: skip


def _out(
    text, camera="static shot", sound="Apart from the spoken lines, the only sounds are rain."
):
    return VideoPromptOutput(
        blocks=[PromptBlock(sub_shot=1, text=text, camera=camera)], sound=sound
    )


def test_the_checks():
    ok = ("A close-up from eye height facing her. The woman from {pic:A} and {pic:B}, in a red "
          'coat, at frame left, says in English: "Go home." Only her lips move.')  # fmt: skip
    problems, advice = prompt_findings(_out(ok), _context(["Go home."]), ["Derek"])
    assert problems == [] and all("words" in a for a in advice)  # only: it is short
    problems, _ = prompt_findings(_out(ok.replace("Go home.", "Go.")), _context(["Go home."]), [])
    assert any("word for word" in p for p in problems)
    problems, _ = prompt_findings(_out(ok + " {pic:Z}"), _context(["Go home."]), [])
    assert any("aren't sent" in p for p in problems)
    problems, _ = prompt_findings(_out(ok.replace(" and {pic:B}", "")), _context(["Go home."]), [])
    assert any("never names {pic:B}" in p for p in problems)
    problems, _ = prompt_findings(_out(ok + " Derek waits outside."), _context(["Go home."]),
                                  ["Derek"])  # fmt: skip
    assert any("Derek" in p for p in problems)
    _, advice = prompt_findings(_out(ok + " She does not smile. He explains."),
                                _context(["Go home."]), [])  # fmt: skip
    assert any("isn't there (not)" in a for a in advice)
    assert any("speaking verb" in a for a in advice)
    _, advice = prompt_findings(_out(ok, camera="slow dolly"), _context(["Go home."]), [])
    assert any("MiniMax's wording" in a for a in advice)
    silent = "A close-up of the woman from {pic:A} and {pic:B} at frame left."
    _, advice = prompt_findings(_out(silent), _context(), [])
    assert any("stay silent" in a for a in advice)


def test_dialogue_pace(tmp_path):
    with Board(tmp_path) as b:
        pkg = b.repo.get_package(b.pid, b.board()["revision_id"])
        talky = max(pkg.shot_list, key=lambda s: len(s.dialogue_cues))
        assert dialogue_pace(pkg, talky, 100) is not None  # a tenth of a second holds nothing
        assert dialogue_pace(pkg, talky, 600_000) is None
