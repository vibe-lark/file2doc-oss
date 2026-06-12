from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import shutil
import subprocess


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_MAX_FRAMES = 12
_CACHE_FILENAME = ".file2doc_video_frames.json"


def extract_video_frames(
    source_path: Path,
    output_dir: Path,
    *,
    command_runner: CommandRunner | None = None,
    new_artifact_id: Callable[[], str] | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> dict:
    runner = command_runner or subprocess.run
    artifact_id_factory = new_artifact_id or _default_artifact_id
    if max_frames < 1:
        raise ValueError("max_frames must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_key = _cache_key(source_path, max_frames)
    cached = _read_cached_result(output_dir, cache_key)
    if cached is not None:
        return cached

    scene_times = _extract_scene_frames(source_path, output_dir, max_frames, runner)
    if not scene_times:
        raise VideoFrameExtractionError("ffmpeg extracted no scene-change frames")
    frame_paths = sorted(output_dir.glob("frame_*.jpg"))[:max_frames]
    if not frame_paths:
        raise VideoFrameExtractionError("ffmpeg extracted no scene-change frames")
    frame_times = scene_times[: len(frame_paths)]
    media_index = []
    timeline = []
    artifacts = {}

    for index, (frame_path, time_seconds) in enumerate(zip(frame_paths, frame_times), start=1):
        relative_path = frame_path.relative_to(output_dir)
        artifact_id = artifact_id_factory()
        media_id = f"video-frame-{index:03d}"
        media = {
            "id": media_id,
            "kind": "video_frame",
            "path": relative_path.as_posix(),
            "artifact_id": artifact_id,
            "media_type": "image/jpeg",
            "source_ref": {"type": "video_time", "time_seconds": time_seconds},
            "derived": False,
            "time_seconds": time_seconds,
        }
        artifact = {
            "artifact_id": artifact_id,
            "kind": "video_frame",
            "path": relative_path.as_posix(),
            "media_type": "image/jpeg",
            "time_seconds": time_seconds,
            "derived": False,
        }
        media_index.append(media)
        artifacts[artifact_id] = artifact
        timeline.append(
            {
                "id": media_id,
                "kind": "video_frame",
                "time_seconds": time_seconds,
                "media_id": media_id,
                "artifact_id": artifact_id,
                "path": relative_path.as_posix(),
            }
        )

    result = {"media_index": media_index, "timeline": timeline, "artifacts": artifacts}
    _write_cached_result(output_dir, cache_key, result)
    return result


def _extract_scene_frames(
    source_path: Path,
    output_dir: Path,
    max_frames: int,
    runner: CommandRunner,
) -> list[float]:
    command = [
        _ffmpeg_executable(),
        "-v",
        "info",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        "select='eq(n\\,0)+gt(scene\\,0.30)',showinfo",
        "-vsync",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(output_dir / "frame_%03d.jpg"),
    ]
    try:
        completed = runner(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise VideoToolUnavailable(f"{command[0]} is required for video frame extraction") from error
    return _parse_showinfo_times(completed.stderr)


def _parse_showinfo_times(stderr: str) -> list[float]:
    return [round(float(match), 3) for match in re.findall(r"pts_time:([0-9.]+)", stderr)]


def _ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise VideoToolUnavailable(
            "ffmpeg or imageio-ffmpeg is required for video frame extraction"
        ) from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def _default_artifact_id() -> str:
    raise VideoFrameExtractionError("new_artifact_id is required")


def _cache_key(source_path: Path, max_frames: int) -> dict:
    stat = source_path.stat()
    return {
        "source_path": str(source_path.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "max_frames": max_frames,
    }


def _read_cached_result(output_dir: Path, cache_key: dict) -> dict | None:
    cache_path = output_dir / _CACHE_FILENAME
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if cached.get("cache_key") != cache_key:
        return None

    result = cached.get("result")
    if not isinstance(result, dict):
        return None
    media_index = result.get("media_index")
    if not isinstance(media_index, list):
        return None
    for item in media_index:
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        if not isinstance(path, str) or not (output_dir / path).exists():
            return None
    return result


def _write_cached_result(output_dir: Path, cache_key: dict, result: dict) -> None:
    cache_path = output_dir / _CACHE_FILENAME
    cache_path.write_text(
        json.dumps({"cache_key": cache_key, "result": result}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class VideoToolUnavailable(RuntimeError):
    pass


class VideoFrameExtractionError(RuntimeError):
    pass
