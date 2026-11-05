import array
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose


@pytest.mark.parametrize("has_audio", [True, False])
@pytest.mark.parametrize("fps", [None, 60])
def test_compose_frame_rate_before_concat(tmp_path, monkeypatch, fps, has_audio):
    source = tmp_path / "source.mp4"
    source.touch()
    commands = []
    tool = VideoCompose()
    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).touch()
        return ""
    monkeypatch.setattr(tool, "run_command", fake_run)
    monkeypatch.setattr(tool, "_has_audio_stream", lambda _: has_audio)
    edit = {"cuts": [{"source": str(source), "in_seconds": 0, "out_seconds": 1}]}
    if fps:
        edit["metadata"] = {"compose_target": {"fps": fps}}
    result = tool.execute({"operation": "compose", "edit_decisions": edit,
                           "output_path": str(tmp_path / "output.mp4")})
    assert result.success, result.error
    trim = commands[0]
    assert f"fps={fps or 30}" in trim[trim.index("-filter:v") + 1]
    assert trim[trim.index("-r") + 1] == str(fps or 30)
    assert trim[trim.index("-c:a") + 1] == "pcm_s16le"
    assert commands[-1][commands[-1].index("-c:a") + 1] == "aac"


@pytest.mark.parametrize("fps", [0, 121, True, "60", float("nan")])
def test_invalid_frame_rate_rejected(tmp_path, fps):
    result = VideoCompose().execute({"operation": "compose", "edit_decisions": {
        "cuts": [], "metadata": {"compose_target": {"fps": fps}}}, "output_path": str(tmp_path / "out.mp4")})
    assert not result.success
    assert "compose_target.fps" in result.error


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg")
def test_concat_does_not_accumulate_aac_priming(tmp_path):
    """A tone must start at the cut, not 3 AAC packets after it."""
    silent, tone = tmp_path / "silent.mp4", tmp_path / "tone.mp4"
    for target, audio, duration in [(silent, "anullsrc=r=48000:cl=stereo", 0.5),
                                    (tone, "sine=frequency=1000:sample_rate=48000", 0.8)]:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=blue:s=64x64:r=60:d={duration}", "-f", "lavfi", "-i", audio,
                        "-t", str(duration), "-c:v", "libx264", "-c:a", "aac", str(target)],
                       check=True, capture_output=True, timeout=30)
    output = tmp_path / "joined.mp4"
    result = VideoCompose().execute({"operation": "compose", "preset": "ultrafast",
        "output_path": str(output), "edit_decisions": {"metadata": {"compose_target": {"fps": 60}},
        "cuts": [{"source": str(silent), "in_seconds": 0, "out_seconds": 0.5},
                 {"source": str(tone), "in_seconds": 0, "out_seconds": 0.8}]}})
    assert result.success, result.error
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vn",
                               "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                              capture_output=True, check=True, timeout=30)
    samples = array.array("h")
    samples.frombytes(decoded.stdout)
    onset = next(i for i, value in enumerate(samples) if abs(value) > 800) / 8000
    assert abs(onset - 0.5) < 1 / 60, onset
