"""Assembling clips: trimmed to their sound, with a music bed under the scenes."""

import subprocess
from pathlib import Path

import pytest

from drama_agent.musicgen import fixture_tone
from drama_agent.shots import assemble as asm
from drama_agent.videogen import ffmpeg_available

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")


def _clip(
    path: Path, seconds: float, sound: tuple[float, float] | None, color: str = "red"
) -> Path:
    """A plain clip with a tone from `sound[0]` to `sound[1]` s, else a silent track."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=256x448:d={seconds}"]  # fmt: skip
    if sound is None:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    else:
        a, b = sound
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                 "-af", f"volume='if(between(t,{a},{b}),1,0)':eval=frame"]  # fmt: skip
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(args, check=True, capture_output=True)
    return path


def test_a_clip_is_trimmed_to_its_sound_with_margins(tmp_path):
    clip = _clip(tmp_path / "talk.mp4", 6, (1.5, 4.0))
    first, last = asm.sound_window(clip)
    assert 1.4 < first < 1.6 and 3.9 < last < 4.2
    start, end = asm.trim_points(clip)
    assert 1.2 <= start <= 1.3 and 4.5 <= end <= 4.8  # 0.25 s before, 0.6 s after
    # A silent clip (an action clip) keeps its whole length.
    silent = _clip(tmp_path / "walk.mp4", 5, None, "blue")
    assert asm.sound_window(silent) is None
    assert asm.trim_points(silent) == (0.0, pytest.approx(5.0, abs=0.1))
    # Sound only at the very end: the clip keeps at least half its length.
    late = _clip(tmp_path / "late.mp4", 6, (5.5, 6.0), "green")
    start, end = asm.trim_points(late)
    assert end - start >= 3.0 - 0.01


def test_the_assembly_applies_the_trims_and_lays_music_under_the_cut(tmp_path):
    talk = _clip(tmp_path / "talk.mp4", 6, (1.5, 4.0))
    walk = _clip(tmp_path / "walk.mp4", 5, None, "blue")
    music = tmp_path / "bed.wav"
    music.write_bytes(fixture_tone(12, 3))
    out = asm.assemble(
        [talk, walk], tmp_path / "ep.mp4", tmp_path / "ep.jpg",
        trims=[asm.trim_points(talk), (0.0, 5.0)],
        music=[asm.MusicBed(music, 0.0, 8.0, -12.0)],
    )  # fmt: skip
    assert out["clips"] == 2 and out["music"] == 1
    assert 2.4 < out["trimmed_s"] < 2.9  # the talk clip's dead air (6 s less 1.25 to 4.6)
    assert abs(out["duration_s"] - (6 - out["trimmed_s"] + 5)) < 0.3
    assert (tmp_path / "ep.jpg").is_file()
    info = asm.probe(tmp_path / "ep.mp4")
    assert info["audio"] and info["width"] == 256
    # With music, the silent walk clip is no longer silent: the bed plays under it.
    window = asm.sound_window(tmp_path / "ep.mp4", noise_db=-50)
    assert window is not None and window[1] > 6.0
    # Without trims and music the whole lengths are kept.
    plain = asm.assemble([talk, walk], tmp_path / "plain.mp4")
    assert plain["trimmed_s"] == 0 and plain["music"] == 0
    assert abs(plain["duration_s"] - 11) < 0.3
    with pytest.raises(asm.AssembleError, match="one trim per clip"):
        asm.assemble([talk, walk], tmp_path / "x.mp4", trims=[(0.0, 1.0)])
