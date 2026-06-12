from pathlib import Path
import subprocess

import pytest

from file2doc.video import VideoToolUnavailable, extract_video_frames


def test_missing_ffmpeg_fails_clearly(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not a real video")

    def missing_runner(command, **kwargs):
        raise FileNotFoundError(command[0])

    with pytest.raises(VideoToolUnavailable) as error:
        extract_video_frames(source, tmp_path / "frames", command_runner=missing_runner)

    assert "ffmpeg" in str(error.value)


def test_fake_runner_creates_frames_and_returns_manifest_metadata(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        if Path(command[0]).name.startswith("ffmpeg"):
            assert any("gt(scene\\,0.30)" in part for part in command)
            Path(str(command[-1]).replace("%03d", "001")).write_bytes(b"frame")
            Path(str(command[-1]).replace("%03d", "002")).write_bytes(b"frame")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="pts_time:0.0 pts_time:3.2",
            )
        raise AssertionError(command)

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=fake_runner,
        new_artifact_id=iter(["artifact-1", "artifact-2"]).__next__,
        max_frames=2,
    )

    assert [Path(call[0]).name for call in calls] == ["ffmpeg"]
    assert [item["id"] for item in result["media_index"]] == ["video-frame-001", "video-frame-002"]
    assert [item["time_seconds"] for item in result["timeline"]] == [0.0, 3.2]
    assert result["media_index"][0] == {
        "id": "video-frame-001",
        "kind": "video_frame",
        "path": "frame_001.jpg",
        "artifact_id": "artifact-1",
        "media_type": "image/jpeg",
        "source_ref": {"type": "video_time", "time_seconds": 0.0},
        "derived": False,
        "time_seconds": 0.0,
    }
    assert result["artifacts"]["artifact-1"] == {
        "artifact_id": "artifact-1",
        "kind": "video_frame",
        "path": "frame_001.jpg",
        "media_type": "image/jpeg",
        "time_seconds": 0.0,
        "derived": False,
    }
    assert (tmp_path / "frames" / "frame_001.jpg").read_bytes() == b"frame"
    assert (tmp_path / "frames" / "frame_002.jpg").read_bytes() == b"frame"


def test_default_frame_cap_extracts_at_most_twelve_frames(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(duration_seconds=30),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 20)).__next__,
    )

    assert len(result["media_index"]) == 12
    assert result["timeline"][-1]["time_seconds"] == 11.0


def test_frame_cap_option_limits_selected_frames(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(duration_seconds=30),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 20)).__next__,
        max_frames=3,
    )

    assert [item["time_seconds"] for item in result["timeline"]] == [0.0, 1.0, 2.0]


def test_repeated_extraction_reuses_existing_frames_when_source_is_unchanged(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command[0])
        if Path(command[0]).name.startswith("ffmpeg"):
            Path(str(command[-1]).replace("%03d", "001")).write_bytes(b"frame")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="pts_time:0.0",
            )
        raise AssertionError(command)

    first = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=fake_runner,
        new_artifact_id=iter(["artifact-1"]).__next__,
    )
    second = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=fake_runner,
        new_artifact_id=iter(["unused-artifact"]).__next__,
    )

    assert first == second
    assert [Path(call).name for call in calls] == ["ffmpeg"]


def _fake_video_runner(duration_seconds):
    def runner(command, **kwargs):
        if Path(command[0]).name.startswith("ffmpeg"):
            for index in range(1, 20):
                Path(str(command[-1]).replace("%03d", f"{index:03d}")).write_bytes(b"frame")
            stderr = " ".join(f"pts_time:{index}.0" for index in range(20))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr=stderr)
        raise AssertionError(command)

    return runner
