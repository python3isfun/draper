"""The speech check: where a clip is sampled, and the rules on who is seen talking."""

from drama_agent.llm import GenerationRequest
from drama_agent.shots.models import SubShot
from drama_agent.shots.speech_check import (
    MAX_FRAMES,
    CutSpeech,
    SpeechOutput,
    face_owner,
    fixture_speech,
    sample_times,
    speech_context,
    speech_findings,
)


def _subs(*spans, people=("ch_a",)):
    return [
        SubShot(index=k, start_ms=a, end_ms=b, framing=f"f{k}", action="x", people=list(people))
        for k, (a, b) in enumerate(spans, 1)
    ]


def _line(speaker_id, speaker, start, end, text="Hi."):
    return {"dialogue_id": f"d{start}", "speaker_id": speaker_id, "speaker": speaker,
            "text": text, "start_ms": start, "end_ms": end}  # fmt: skip


def test_frames_are_sampled_inside_every_line_and_over_a_silent_cut():
    subs = _subs((0, 4000), (4000, 8000))
    lines = [_line("ch_a", "Ann", 500, 3500)]
    [(one, t1), (two, t2)] = sample_times(subs, lines)
    assert one == 1 and len(t1) == 3 and all(0.5 < t < 3.5 for t in t1)
    assert two == 2 and len(t2) == 2 and all(4 < t < 8 for t in t2)
    # Many lines: capped, the longest lists trimmed first.
    lines = [_line("ch_a", "Ann", k * 500, k * 500 + 400) for k in range(8)]
    out = sample_times(_subs((0, 4000), (4000, 8000)), lines)
    assert sum(len(t) for _, t in out) == MAX_FRAMES and all(t for _, t in out)


def test_the_context_names_the_faces_and_the_frames():
    subs = _subs((0, 4000), people=("ch_a", "ch_b"))
    lines = [_line("ch_a", "Ann", 500, 3500)]
    ctx = speech_context({"ch_a": "Ann", "ch_b": "Bo"}, ["ch_a", "ch_b"], subs, lines,
                         sample_times(subs, lines), {"ch_b": 1})  # fmt: skip
    assert ctx["people"] == [{"name": "Ann", "face": None}, {"name": "Bo", "face": "<Picture 1>"}]
    [cut] = ctx["cuts"]
    assert cut["frames"] == ["<Cut 1 frame 1>", "<Cut 1 frame 2>", "<Cut 1 frame 3>"]
    assert cut["in_frame"] == ["Ann", "Bo"]
    assert cut["lines"] == [{"speaker": "Ann", "line": "Hi.", "where": "on screen"}]


def _cuts(where="on screen", in_frame=("Ann", "Bo"), lines=True):
    return [{"sub_shot": 1, "in_frame": list(in_frame),
             "lines": [{"speaker": "Ann", "line": "Hi.", "where": where}] if lines else []}]  # fmt: skip


def _said(*names, note="seen"):
    return SpeechOutput(cuts=[CutSpeech(sub_shot=1, speaking=list(names), note=note)])


def test_the_speaker_and_nobody_else_is_seen_speaking():
    ok = speech_findings(_said("Ann"), _cuts())
    assert [(i.item, i.passed) for i in ok] == [
        ("cut 1: Ann speaks", True), ("cut 1: nobody else speaks", True)]  # fmt: skip
    # The wrong face talks: major.
    wrong = speech_findings(_said("Bo"), _cuts())
    assert [(i.passed, i.severity) for i in wrong] == [(False, "minor"), (False, "major")]
    assert wrong[1].finding == "Bo is seen speaking on Ann's line (seen)"
    # Nobody is seen speaking: minor (a sampled frame can fall between words).
    none = speech_findings(_said(), _cuts())
    assert [(i.passed, i.severity) for i in none] == [(False, "minor"), (True, "major")]
    # Names are matched loosely: a first name, another case, a surname added.
    loose = speech_findings(_said("ann", "Bo Peep"), _cuts(in_frame=("Ann Lee", "Bo")))
    assert loose[0].passed and loose[1].finding.startswith("Bo is seen speaking")
    # Someone the model can't place is reported as given.
    other = speech_findings(_said("someone else"), _cuts())
    assert other[1].finding.startswith("someone else is seen speaking on Ann's line")


def test_a_cut_with_no_line_in_frame_must_be_silent():
    quiet = speech_findings(_said(), _cuts(where="off screen left"))
    assert [(i.item, i.passed) for i in quiet] == [("cut 1: silent in frame", True)]
    talks = speech_findings(_said("Bo"), _cuts(where="off screen left"))
    assert not talks[0].passed and talks[0].severity == "major"
    assert talks[0].finding == "Bo is seen speaking, but the line is spoken off screen (seen)"
    talks = speech_findings(_said("Ann", "Bo"), _cuts(lines=False))
    assert talks[0].finding == "Ann and Bo are seen speaking, but the cut has no line (seen)"
    # A cut the model didn't report on counts as nobody seen.
    missing = speech_findings(SpeechOutput(cuts=[]), _cuts())
    assert missing[0].finding == "Ann isn't seen speaking (not reported)"


def test_the_offline_check_passes_everyone():
    request = GenerationRequest(stage="speech_check", instructions="", context={  # type: ignore[arg-type]
        "cuts": [*_cuts(), {"sub_shot": 2, "in_frame": ["Bo"], "lines": [
            {"speaker": "Ann", "line": "Hi.", "where": "off screen left"}]}]})  # fmt: skip
    out = fixture_speech(request)
    assert [(c.sub_shot, c.speaking) for c in out.cuts] == [(1, ["Ann"]), (2, [])]
    items = speech_findings(out, request.context["cuts"])
    assert all(i.passed for i in items)


def test_a_face_picture_is_matched_to_its_person_by_what_it_is_about():
    names = {"ch_a": "Ann Lee", "ch_b": "Bo"}
    assert face_owner("Ann Lee's face", names) == "ch_a"
    assert face_owner("Ann Lee", names) == "ch_a"
    assert face_owner("Bo, full body", names) is None  # not a bare name or "X's ..."
    assert face_owner("Bo's face", names) == "ch_b"
    assert face_owner("the set, wide view", names) is None
