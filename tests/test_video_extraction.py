from pathlib import Path
import subprocess

from PIL import Image
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


def test_returns_manifest_metadata_for_perceptual_frames(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    runner = _fake_video_runner(
        duration_seconds=8,
        coverage_times=[0.0, 2.0, 4.0, 6.0],
        scene_times=[0.0, 3.2],
    )

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=runner,
        new_artifact_id=(f"artifact-{index}" for index in range(1, 20)).__next__,
    )

    assert len(result["media_index"]) >= 2
    first = result["media_index"][0]
    assert first == {
        "id": "video-frame-001",
        "kind": "video_frame",
        "path": "frame_001.jpg",
        "artifact_id": "artifact-1",
        "media_type": "image/jpeg",
        "source_ref": {"type": "video_time", "time_seconds": 0.0},
        "derived": False,
        "time_seconds": 0.0,
    }
    assert result["timeline"][0]["media_id"] == "video-frame-001"
    assert result["artifacts"]["artifact-1"]["time_seconds"] == 0.0
    assert (tmp_path / "frames" / "frame_001.jpg").is_file()


def test_default_selection_uses_content_instead_of_a_frame_quota(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(
            duration_seconds=30,
            coverage_times=[0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
            scene_times=[0.0],
            identical_frames=True,
        ),
        new_artifact_id=iter(["artifact-1"]).__next__,
    )

    assert [item["time_seconds"] for item in result["timeline"]] == [0.0]


def test_sensitive_perceptual_selection_keeps_visual_changes(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    times = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(
            duration_seconds=12,
            coverage_times=times,
            scene_times=[0.0],
        ),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 20)).__next__,
    )

    assert len(result["timeline"]) >= 4
    assert result["timeline"][0]["time_seconds"] == 0.0
    assert result["timeline"][-1]["time_seconds"] == 10.0


def test_explicit_frame_cap_preserves_time_coverage(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(
            duration_seconds=20,
            coverage_times=[0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0],
            scene_times=[0.0],
        ),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 10)).__next__,
        max_frames=3,
    )

    times = [item["time_seconds"] for item in result["timeline"]]
    assert len(times) == 3
    assert times[0] < 7
    assert 7 <= times[1] < 14
    assert times[2] >= 14


def test_low_information_candidates_are_removed_when_usable_frames_exist(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(
            duration_seconds=8,
            coverage_times=[0.0, 2.0, 4.0, 6.0],
            scene_times=[],
            flat_frame_times={2.0, 6.0},
        ),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 10)).__next__,
    )

    assert [item["time_seconds"] for item in result["timeline"]] == [0.0, 4.0]


def test_duration_falls_back_to_ffmpeg_when_ffprobe_is_missing(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr("file2doc.video._ffprobe_executable", lambda: "missing-ffprobe")
    monkeypatch.setattr("file2doc.video._ffmpeg_executable", lambda: "imageio-ffmpeg")

    result = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=_fake_video_runner(
            duration_seconds=24.83,
            coverage_times=[0.0, 2.0, 4.0, 6.0],
            scene_times=[0.0],
            ffprobe_missing=True,
        ),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 10)).__next__,
    )

    assert result["timeline"][-1]["time_seconds"] == 6.0


def test_candidate_images_are_removed_after_materialization(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    output_dir = tmp_path / "frames"

    extract_video_frames(
        source,
        output_dir,
        command_runner=_fake_video_runner(
            duration_seconds=8,
            coverage_times=[0.0, 2.0, 4.0, 6.0],
            scene_times=[0.0, 3.0],
        ),
        new_artifact_id=(f"artifact-{index}" for index in range(1, 10)).__next__,
    )

    assert list(output_dir.glob("coverage_*.jpg")) == []
    assert list(output_dir.glob("scene_*.jpg")) == []
    assert list(output_dir.glob("frame_*.jpg"))


def test_repeated_extraction_reuses_existing_frames(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    calls = []
    runner = _fake_video_runner(
        duration_seconds=10,
        coverage_times=[0.0, 2.0, 4.0, 6.0, 8.0],
        scene_times=[0.0],
        calls=calls,
    )

    first = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=runner,
        new_artifact_id=(f"artifact-{index}" for index in range(1, 10)).__next__,
    )
    second = extract_video_frames(
        source,
        tmp_path / "frames",
        command_runner=runner,
        new_artifact_id=iter(["unused-artifact"]).__next__,
    )

    assert first == second
    assert calls == ["ffprobe", "coverage", "scene"]


def _fake_video_runner(
    *,
    duration_seconds,
    coverage_times,
    scene_times,
    identical_frames=False,
    flat_frame_times=None,
    ffprobe_missing=False,
    calls=None,
):
    flat_frame_times = flat_frame_times or set()

    def runner(command, **kwargs):
        if Path(command[0]).name.startswith("ffprobe"):
            if ffprobe_missing:
                raise FileNotFoundError(command[0])
            if calls is not None:
                calls.append("ffprobe")
            return subprocess.CompletedProcess(command, 0, stdout=str(duration_seconds), stderr="")
        if command[:2] == ["imageio-ffmpeg", "-i"]:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=f"Duration: 00:00:{duration_seconds:05.2f}, start: 0.000000",
            )
        if Path(command[0]).name.startswith("ffmpeg") or command[0] == "imageio-ffmpeg":
            is_coverage = any(str(part).startswith("fps=") for part in command)
            if calls is not None:
                calls.append("coverage" if is_coverage else "scene")
            times = coverage_times if is_coverage else scene_times
            max_frames = int(command[command.index("-frames:v") + 1])
            selected_times = list(times[:max_frames])
            for index, time_seconds in enumerate(selected_times, start=1):
                target = Path(str(command[-1]).replace("%03d", f"{index:03d}"))
                _write_test_frame(
                    target,
                    seed=0 if identical_frames else int(round(time_seconds)),
                    flat=time_seconds in flat_frame_times,
                )
            stderr = " ".join(f"pts_time:{time_seconds}" for time_seconds in selected_times)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr=stderr)
        raise AssertionError(command)

    return runner


def _write_test_frame(path: Path, *, seed: int, flat: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if flat:
        Image.new("RGB", (64, 64), (0, 0, 0)).save(path, format="JPEG")
        return
    image = Image.new("L", (64, 64))
    image.putdata(
        [
            235 if ((x * 17 + y * 31 + seed * 47) % 101) < 50 else 20
            for y in range(64)
            for x in range(64)
        ]
    )
    image.convert("RGB").save(path, format="JPEG", quality=95)
