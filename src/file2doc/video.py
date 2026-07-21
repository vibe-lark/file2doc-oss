from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess

from PIL import Image, ImageFilter, ImageStat


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_CANDIDATE_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_CANDIDATES = 360
DEFAULT_NOVELTY_THRESHOLD = 4
_CACHE_FILENAME = ".file2doc_video_frames.json"
_CACHE_STRATEGY_VERSION = 4


@dataclass(frozen=True)
class _FrameCandidate:
    path: Path
    time_seconds: float
    reason: str
    brightness: float = 0.0
    entropy: float = 0.0
    sharpness: float = 0.0
    ahash: int = 0
    diff_from_prev: int = 64


def extract_video_frames(
    source_path: Path,
    output_dir: Path,
    *,
    command_runner: CommandRunner | None = None,
    new_artifact_id: Callable[[], str] | None = None,
    max_frames: int | None = None,
) -> dict:
    runner = command_runner or subprocess.run
    artifact_id_factory = new_artifact_id or _default_artifact_id
    if max_frames is not None and max_frames < 1:
        raise ValueError("max_frames must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_key = _cache_key(source_path, max_frames)
    cached = _read_cached_result(output_dir, cache_key)
    if cached is not None:
        return cached

    _clear_extracted_frames(output_dir)
    duration_seconds = _probe_duration_seconds(source_path, runner)
    candidate_limit = max(DEFAULT_MAX_CANDIDATES, max_frames or 0)
    interval_seconds = max(
        DEFAULT_CANDIDATE_INTERVAL_SECONDS,
        (duration_seconds or 0.0) / DEFAULT_MAX_CANDIDATES,
    )
    coverage_frames = _extract_interval_frames(
        source_path,
        output_dir,
        candidate_limit,
        interval_seconds,
        runner,
    )
    scene_frames = _extract_scene_frames(source_path, output_dir, candidate_limit, runner)
    candidate_frames = _merge_frame_candidates(coverage_frames + scene_frames)
    scored_candidates = _score_frame_candidates(candidate_frames)
    selected_candidates = _select_perceptual_candidates(scored_candidates)
    if max_frames is not None and len(selected_candidates) > max_frames:
        selected_candidates = _limit_frame_candidates(
            selected_candidates,
            max_frames,
            duration_seconds,
        )
    frame_paths, frame_times = _materialize_frame_candidates(
        selected_candidates,
        output_dir,
    )
    _clear_candidate_frames(output_dir)
    if not frame_paths:
        raise VideoFrameExtractionError("ffmpeg extracted no video frames")
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


def _probe_duration_seconds(source_path: Path, runner: CommandRunner) -> float | None:
    ffprobe_duration = _probe_duration_seconds_with_ffprobe(source_path, runner)
    if ffprobe_duration is not None:
        return ffprobe_duration
    return _probe_duration_seconds_with_ffmpeg(source_path, runner)


def _probe_duration_seconds_with_ffprobe(
    source_path: Path,
    runner: CommandRunner,
) -> float | None:
    command = [
        _ffprobe_executable(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    try:
        completed = runner(command, check=True, capture_output=True, text=True)
    except Exception:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _probe_duration_seconds_with_ffmpeg(
    source_path: Path,
    runner: CommandRunner,
) -> float | None:
    command = [_ffmpeg_executable(), "-i", str(source_path)]
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except Exception:
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _extract_scene_frames(
    source_path: Path,
    output_dir: Path,
    max_frames: int,
    runner: CommandRunner,
) -> list[_FrameCandidate]:
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
        str(output_dir / "scene_%03d.jpg"),
    ]
    try:
        completed = runner(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise VideoToolUnavailable(f"{command[0]} is required for video frame extraction") from error
    return _frame_candidates(output_dir, "scene_*.jpg", _parse_showinfo_times(completed.stderr))


def _extract_interval_frames(
    source_path: Path,
    output_dir: Path,
    max_frames: int,
    interval_seconds: float,
    runner: CommandRunner,
) -> list[_FrameCandidate]:
    fps = 1.0 / max(interval_seconds, 0.1)
    command = [
        _ffmpeg_executable(),
        "-v",
        "info",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        f"fps={fps:.8f},showinfo",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(output_dir / "coverage_%03d.jpg"),
    ]
    try:
        completed = runner(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise VideoToolUnavailable(f"{command[0]} is required for video frame extraction") from error
    return _frame_candidates(
        output_dir,
        "coverage_*.jpg",
        _parse_showinfo_times(completed.stderr),
        reason="coverage_sample",
    )


def _parse_showinfo_times(stderr: str) -> list[float]:
    return [round(float(match), 3) for match in re.findall(r"pts_time:([0-9.]+)", stderr)]


def _frame_candidates(
    output_dir: Path,
    pattern: str,
    times: list[float],
    *,
    reason: str = "scene_change",
) -> list[_FrameCandidate]:
    paths = sorted(output_dir.glob(pattern))
    return [
        _FrameCandidate(path=path, time_seconds=time_seconds, reason=reason)
        for path, time_seconds in zip(paths, times[: len(paths)], strict=False)
    ]


def _merge_frame_candidates(
    candidates: list[_FrameCandidate],
) -> list[_FrameCandidate]:
    merged: list[_FrameCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.time_seconds):
        existing_index = next(
            (
                index
                for index, item in enumerate(merged)
                if abs(item.time_seconds - candidate.time_seconds) < 0.75
            ),
            None,
        )
        if existing_index is None:
            merged.append(candidate)
        elif merged[existing_index].reason != "scene_change" and candidate.reason == "scene_change":
            merged[existing_index] = candidate
    return merged


def _score_frame_candidates(candidates: list[_FrameCandidate]) -> list[_FrameCandidate]:
    scored = []
    previous_hash: int | None = None
    for candidate in candidates:
        with Image.open(candidate.path) as source_image:
            image = source_image.convert("RGB")
            grayscale = image.convert("L")
            brightness = ImageStat.Stat(grayscale).mean[0] * 100.0 / 255.0
            entropy = grayscale.entropy()
            edges = grayscale.filter(ImageFilter.FIND_EDGES)
            sharpness = ImageStat.Stat(edges).var[0]
            ahash = _average_hash(image)
        diff_from_prev = _hamming_distance(ahash, previous_hash)
        previous_hash = ahash
        scored.append(
            _FrameCandidate(
                path=candidate.path,
                time_seconds=candidate.time_seconds,
                reason=candidate.reason,
                brightness=brightness,
                entropy=entropy,
                sharpness=sharpness,
                ahash=ahash,
                diff_from_prev=diff_from_prev,
            )
        )
    return scored


def _select_perceptual_candidates(candidates: list[_FrameCandidate]) -> list[_FrameCandidate]:
    usable = [
        candidate
        for candidate in candidates
        if 8.0 <= candidate.brightness <= 95.0
        and candidate.entropy >= 0.3
        and candidate.sharpness >= 2.0
    ]
    candidates_to_select = usable or candidates
    selected: list[_FrameCandidate] = []
    for candidate in candidates_to_select:
        if (
            selected
            and candidate.reason != "scene_change"
            and candidate.diff_from_prev < DEFAULT_NOVELTY_THRESHOLD
        ):
            continue
        if _is_near_duplicate(candidate, selected):
            continue
        selected.append(candidate)
    return selected


def _is_near_duplicate(
    candidate: _FrameCandidate,
    selected: list[_FrameCandidate],
) -> bool:
    return any(
        abs(candidate.time_seconds - item.time_seconds) < 1.0
        or _hamming_distance(candidate.ahash, item.ahash) <= 4
        for item in selected
    )


def _average_hash(image: Image.Image) -> int:
    pixels = list(image.convert("L").resize((8, 8)).get_flattened_data())
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= average)
    return value


def _hamming_distance(left: int | None, right: int | None) -> int:
    if left is None or right is None:
        return 64
    return (left ^ right).bit_count()


def _limit_frame_candidates(
    candidates: list[_FrameCandidate],
    max_frames: int,
    duration_seconds: float | None,
) -> list[_FrameCandidate]:
    if max_frames >= len(candidates):
        return candidates
    duration = duration_seconds or candidates[-1].time_seconds or float(len(candidates))
    selected = []
    for bucket in range(max_frames):
        start = duration * bucket / max_frames
        end = duration * (bucket + 1) / max_frames
        bucket_candidates = [
            candidate for candidate in candidates if start <= candidate.time_seconds < end
        ]
        if bucket_candidates:
            selected.append(max(bucket_candidates, key=_selection_score))
    if len(selected) < max_frames:
        for candidate in sorted(candidates, key=_selection_score, reverse=True):
            if candidate in selected:
                continue
            selected.append(candidate)
            if len(selected) >= max_frames:
                break
    return sorted(selected, key=lambda item: item.time_seconds)


def _selection_score(candidate: _FrameCandidate) -> float:
    entropy_score = min(candidate.entropy / 8.0, 1.0)
    sharpness_score = min(candidate.sharpness / 600.0, 1.0)
    brightness_penalty = abs(candidate.brightness - 52.0) / 52.0
    diff_score = min(candidate.diff_from_prev / 32.0, 1.0)
    scene_bonus = 0.2 if candidate.reason == "scene_change" else 0.0
    return (
        entropy_score * 0.25
        + sharpness_score * 0.2
        + diff_score * 0.45
        + scene_bonus
        - brightness_penalty * 0.1
    )


def _materialize_frame_candidates(
    candidates: list[_FrameCandidate],
    output_dir: Path,
) -> tuple[list[Path], list[float]]:
    frame_paths = []
    frame_times = []
    for index, candidate in enumerate(candidates, start=1):
        target_path = output_dir / f"frame_{index:03d}.jpg"
        if candidate.path != target_path:
            shutil.copyfile(candidate.path, target_path)
        frame_paths.append(target_path)
        frame_times.append(candidate.time_seconds)
    return frame_paths, frame_times


def _clear_extracted_frames(output_dir: Path) -> None:
    for pattern in ("frame_*.jpg", "scene_*.jpg", "coverage_*.jpg", "uniform_*.jpg"):
        for path in output_dir.glob(pattern):
            path.unlink()


def _clear_candidate_frames(output_dir: Path) -> None:
    for pattern in ("scene_*.jpg", "coverage_*.jpg", "uniform_*.jpg"):
        for path in output_dir.glob(pattern):
            path.unlink()


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


def _ffprobe_executable() -> str:
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe
    return "ffprobe"


def _default_artifact_id() -> str:
    raise VideoFrameExtractionError("new_artifact_id is required")


def _cache_key(source_path: Path, max_frames: int | None) -> dict:
    stat = source_path.stat()
    return {
        "strategy_version": _CACHE_STRATEGY_VERSION,
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
