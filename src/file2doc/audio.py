from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
import array
import shutil
import subprocess
import tempfile
from time import perf_counter
from typing import Any
import wave


AudioRunner = Callable[[Path, "AudioParseOptions"], Any]
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
_VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv"}


@dataclass(frozen=True)
class TranscriptSegment:
    start_sec: float | None
    end_sec: float | None
    text: str
    segment_id: str | None = None

    def to_manifest_segment(self, index: int) -> dict:
        segment = {
            "id": self.segment_id or f"seg-{index}",
            "text": self.text,
        }
        if self.start_sec is not None:
            segment["start_sec"] = self.start_sec
        if self.end_sec is not None:
            segment["end_sec"] = self.end_sec
        return segment


@dataclass(frozen=True)
class AudioTranscript:
    text: str
    segments: tuple[TranscriptSegment, ...] = ()
    engine: str = "local-asr"
    time_alignment_method: str | None = None
    diagnostics: dict = field(default_factory=dict)

    @property
    def time_aligned(self) -> bool:
        return bool(self.segments) and all(
            segment.start_sec is not None and segment.end_sec is not None
            for segment in self.segments
        )

    def to_manifest_transcript(self) -> dict:
        return {
            "text": self.text,
            "time_aligned": self.time_aligned,
            "time_alignment_method": self.time_alignment_method,
            "engine": self.engine,
            "segments": [
                segment.to_manifest_segment(index)
                for index, segment in enumerate(self.segments, start=1)
            ],
        }


@dataclass(frozen=True)
class AudioParseOptions:
    model_dir: Path | None = None
    runner: AudioRunner | None = None
    num_threads: int = 4
    ffmpeg_timeout_sec: int = 300


class AudioParseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_audio_transcript(
    source_path: Path,
    content_type: str,
    options: AudioParseOptions | None = None,
) -> AudioTranscript:
    started_at = perf_counter()
    parse_options = options or AudioParseOptions()

    if not (
        content_type.startswith("audio/")
        or content_type.startswith("video/")
        or source_path.suffix.lower() in _AUDIO_SUFFIXES | _VIDEO_SUFFIXES
    ):
        raise AudioParseFailure(
            "unsupported_audio_content_type",
            f"Audio transcript parsing does not support content type {content_type!r}",
        )

    model_dir = _configured_model_dir(parse_options)
    if model_dir is None:
        raise AudioParseFailure(
            "local_asr_not_configured",
            "Audio transcript parsing requires a local ASR model directory. "
            "Set FILE2DOC_LOCAL_ASR_MODEL_DIR or pass AudioParseOptions(model_dir=...).",
        )

    effective_options = replace(parse_options, model_dir=model_dir)
    runner = effective_options.runner or _run_sherpa_onnx
    transcript = _coerce_transcript(runner(source_path, effective_options))
    if not transcript.text.strip():
        raise AudioParseFailure("empty_transcript", "Local ASR produced no usable transcript.")
    return replace(
        transcript,
        diagnostics={
            **transcript.diagnostics,
            "elapsed_ms": _elapsed_ms(started_at),
            "remote_services_used": False,
        },
    )


def _configured_model_dir(options: AudioParseOptions) -> Path | None:
    if options.model_dir is not None:
        return options.model_dir

    configured = os.environ.get("FILE2DOC_LOCAL_ASR_MODEL_DIR")
    if not configured:
        return None
    return Path(configured)


def _run_sherpa_onnx(source_path: Path, options: AudioParseOptions) -> AudioTranscript:
    model_dir = options.model_dir
    if model_dir is None:
        raise AudioParseFailure(
            "local_asr_not_configured",
            "Audio transcript parsing requires a local ASR model directory.",
        )
    if not model_dir.exists():
        raise AudioParseFailure(
            "local_asr_model_missing",
            f"Configured local ASR model directory does not exist: {model_dir}",
        )
    model_path, tokens_path = _resolve_model_files(model_dir)

    try:
        import sherpa_onnx
    except ImportError as error:
        raise AudioParseFailure(
            "local_asr_runtime_missing",
            "Audio transcript parsing requires the local sherpa-onnx runtime. "
            "Install sherpa-onnx in the service environment or inject an AudioRunner.",
        ) from error

    with tempfile.TemporaryDirectory(prefix="file2doc-asr-") as tmpdir:
        wav_path = Path(tmpdir) / "audio_16k.wav"
        _convert_to_wav(source_path, wav_path, options)
        return _transcribe_wav(
            sherpa_onnx,
            model_path,
            tokens_path,
            wav_path,
            options,
        )


def _resolve_model_files(model_dir: Path) -> tuple[Path, Path]:
    candidates = [
        model_dir,
        model_dir / "sherpa-onnx-paraformer-zh-2023-03-28",
    ]
    for candidate in candidates:
        model_path = candidate / "model.int8.onnx"
        tokens_path = candidate / "tokens.txt"
        if model_path.is_file() and tokens_path.is_file():
            return model_path, tokens_path
    raise AudioParseFailure(
        "local_asr_model_incomplete",
        "Configured local ASR model directory must contain model.int8.onnx and tokens.txt.",
    )


def _convert_to_wav(source_path: Path, wav_path: Path, options: AudioParseOptions) -> None:
    command = [
        _ffmpeg_executable(),
        "-y",
        "-i",
        str(source_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(wav_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=options.ffmpeg_timeout_sec,
        )
    except subprocess.TimeoutExpired as error:
        raise AudioParseFailure("ffmpeg_timeout", "ffmpeg audio extraction timed out.") from error
    except subprocess.CalledProcessError as error:
        raise AudioParseFailure(
            "ffmpeg_failed",
            f"ffmpeg audio extraction failed: {error.stderr.strip()}",
        ) from error
    if not wav_path.is_file():
        raise AudioParseFailure("ffmpeg_failed", "ffmpeg did not produce a WAV file.")


def _ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise AudioParseFailure(
            "ffmpeg_unavailable",
            "Audio transcript parsing requires ffmpeg or imageio-ffmpeg.",
        ) from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def _transcribe_wav(
    sherpa_onnx,
    model_path: Path,
    tokens_path: Path,
    wav_path: Path,
    options: AudioParseOptions,
) -> AudioTranscript:
    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(model_path),
            tokens=str(tokens_path),
            num_threads=options.num_threads,
            sample_rate=16000,
            feature_dim=80,
        )
    except Exception as error:
        raise AudioParseFailure(
            "local_asr_recognizer_failed",
            f"Creating sherpa-onnx recognizer failed: {error}",
        ) from error

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            if (
                wav_file.getnchannels() != 1
                or wav_file.getsampwidth() != 2
                or wav_file.getframerate() != 16000
            ):
                raise AudioParseFailure(
                    "invalid_asr_wav",
                    "ASR WAV input must be 16 kHz mono 16-bit PCM.",
                )
            frames = wav_file.readframes(wav_file.getnframes())
            duration_sec = wav_file.getnframes() / 16000
    except AudioParseFailure:
        raise
    except Exception as error:
        raise AudioParseFailure("invalid_asr_wav", f"Reading ASR WAV failed: {error}") from error

    samples = array.array("h", frames)
    samples_float = [sample / 32768.0 for sample in samples]
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples_float)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()

    return AudioTranscript(
        text=text,
        segments=(),
        engine="sherpa-onnx-local",
        diagnostics={"duration_sec": round(duration_sec, 2)},
    )


def _coerce_transcript(result: Any) -> AudioTranscript:
    if isinstance(result, AudioTranscript):
        return result

    segments = tuple(_coerce_segment(segment) for segment in result.get("segments", ()))
    return AudioTranscript(
        text=str(result.get("text", "")),
        segments=segments,
        engine=str(result.get("engine", "local-asr")),
        time_alignment_method=result.get("time_alignment_method"),
        diagnostics=dict(result.get("diagnostics", {})),
    )


def _coerce_segment(segment: Mapping[str, Any] | Sequence[Any]) -> TranscriptSegment:
    if isinstance(segment, Mapping):
        return TranscriptSegment(
            start_sec=_optional_float(segment.get("start_sec")),
            end_sec=_optional_float(segment.get("end_sec")),
            text=str(segment.get("text", "")),
            segment_id=_optional_str(segment.get("id")),
        )

    if len(segment) != 3:
        raise AudioParseFailure(
            "invalid_asr_result",
            "AudioRunner segment sequences must contain start_sec, end_sec, and text.",
        )
    start_sec, end_sec, text = segment
    return TranscriptSegment(
        start_sec=_optional_float(start_sec),
        end_sec=_optional_float(end_sec),
        text=str(text),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)
