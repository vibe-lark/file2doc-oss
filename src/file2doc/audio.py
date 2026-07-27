from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import perf_counter
from typing import Any


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
    engine: str | None = None
    num_threads: int = 4
    ffmpeg_timeout_sec: int = 300
    asr_chunk_seconds: int = 30
    hotword: str = ""


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
    runner = effective_options.runner or _configured_runner(effective_options)
    transcript = _coerce_transcript(runner(source_path, effective_options))
    return replace(
        transcript,
        diagnostics={
            **transcript.diagnostics,
            "empty_result": not transcript.text.strip(),
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


def _configured_engine(options: AudioParseOptions) -> str:
    configured = options.engine or os.environ.get("FILE2DOC_LOCAL_ASR_ENGINE")
    if not configured:
        return "funasr-local"
    return configured.strip().lower()


def _configured_runner(options: AudioParseOptions) -> AudioRunner:
    engine = _configured_engine(options)
    if engine in {"funasr", "funasr-local"}:
        return _run_funasr
    raise AudioParseFailure(
        "unsupported_local_asr_engine",
        f"Unsupported local ASR engine {engine!r}. Use 'funasr-local'.",
    )


def _run_funasr(source_path: Path, options: AudioParseOptions) -> AudioTranscript:
    model_dir = options.model_dir
    if model_dir is None:
        raise AudioParseFailure(
            "local_asr_not_configured",
            "FunASR transcription requires a local ASR model directory.",
        )
    if not model_dir.exists():
        raise AudioParseFailure(
            "local_asr_model_missing",
            f"Configured FunASR model directory does not exist: {model_dir}",
        )
    try:
        from funasr import AutoModel
    except ImportError as error:
        raise AudioParseFailure(
            "local_asr_runtime_missing",
            "FunASR transcription requires the local funasr runtime. "
            "Install funasr in the service environment or inject an AudioRunner.",
        ) from error

    with tempfile.TemporaryDirectory(prefix="file2doc-funasr-") as tmpdir:
        wav_path = Path(tmpdir) / "audio_16k.wav"
        _convert_to_wav(source_path, wav_path, options)
        started_at = perf_counter()
        try:
            model_kwargs = {
                "model": _funasr_model_ref(
                    model_dir,
                    "FILE2DOC_FUNASR_MODEL",
                    "paraformer-zh",
                ),
                "vad_model": _funasr_model_ref(
                    model_dir,
                    "FILE2DOC_FUNASR_VAD_MODEL",
                    "fsmn-vad",
                ),
                "disable_update": True,
            }
            punc_model = _optional_funasr_model_ref(
                model_dir,
                "FILE2DOC_FUNASR_PUNC_MODEL",
                "ct-punc",
            )
            if punc_model is not None:
                model_kwargs["punc_model"] = punc_model
            model = AutoModel(**model_kwargs)
            generated = model.generate(
                input=str(wav_path),
                batch_size_s=_funasr_batch_size_seconds(),
                sentence_timestamp=True,
                return_raw_text=True,
                hotword=options.hotword or os.environ.get("FILE2DOC_FUNASR_HOTWORD", ""),
            )
        except Exception as error:
            raise AudioParseFailure(
                "local_asr_recognizer_failed",
                f"FunASR transcription failed: {error}",
            ) from error

    if not generated:
        return AudioTranscript(
            text="",
            segments=(),
            engine="funasr-local",
            time_alignment_method="funasr_sentence_timestamp",
            diagnostics={
                "result_count": 0,
                "asr_engine_elapsed_ms": _elapsed_ms(started_at),
            },
        )

    first = generated[0]
    raw_text = str(first.get("text") or "").strip()
    segments = _segments_from_funasr_sentence_info(first.get("sentence_info") or ())
    if not segments:
        segments = _segments_from_funasr_timestamps(raw_text, first.get("timestamp") or ())
    text = _clean_funasr_text(raw_text) or "\n".join(segment.text for segment in segments).strip()
    return AudioTranscript(
        text=text,
        segments=segments,
        engine="funasr-local",
        time_alignment_method="funasr_sentence_timestamp",
        diagnostics={
            "result_count": len(generated),
            "sentence_count": len(segments),
            "timestamp_count": len(first.get("timestamp") or []),
            "asr_engine_elapsed_ms": _elapsed_ms(started_at),
        },
    )


def _segments_from_funasr_sentence_info(sentence_info: Sequence[Mapping[str, Any]]) -> tuple[TranscriptSegment, ...]:
    return tuple(
        TranscriptSegment(
            start_sec=round(float(sentence["start"]) / 1000, 3),
            end_sec=round(float(sentence["end"]) / 1000, 3),
            text=_clean_funasr_text(str(sentence.get("text", "")).strip()),
            segment_id=f"seg-{index}",
        )
        for index, sentence in enumerate(sentence_info, start=1)
        if _clean_funasr_text(str(sentence.get("text", "")).strip())
    )


def _segments_from_funasr_timestamps(text: str, timestamps: Sequence[Any]) -> tuple[TranscriptSegment, ...]:
    tokens = _funasr_text_tokens(text)
    pairs = [_timestamp_pair(timestamp) for timestamp in timestamps]
    aligned = [(token, pair) for token, pair in zip(tokens, pairs, strict=False) if pair is not None]
    if not aligned:
        return ()

    segments: list[TranscriptSegment] = []
    current_tokens: list[str] = []
    start_ms: float | None = None
    end_ms: float | None = None
    for token, (token_start_ms, token_end_ms) in aligned:
        if start_ms is None:
            start_ms = token_start_ms
        current_tokens.append(token)
        end_ms = token_end_ms
        elapsed_sec = (end_ms - start_ms) / 1000
        should_flush = (
            token in _FUNASR_SENTENCE_BOUNDARY_TOKENS
            or (elapsed_sec >= 8 and len(current_tokens) >= 12)
            or len(current_tokens) >= 40
        )
        if should_flush:
            _append_timestamp_segment(segments, current_tokens, start_ms, end_ms)
            current_tokens = []
            start_ms = None
            end_ms = None

    if current_tokens and start_ms is not None and end_ms is not None:
        _append_timestamp_segment(segments, current_tokens, start_ms, end_ms)
    return tuple(
        replace(segment, segment_id=f"seg-{index}")
        for index, segment in enumerate(segments, start=1)
    )


def _append_timestamp_segment(
    segments: list[TranscriptSegment],
    tokens: Sequence[str],
    start_ms: float,
    end_ms: float,
) -> None:
    segment_text = _join_funasr_tokens(tokens)
    if not segment_text:
        return
    segments.append(
        TranscriptSegment(
            start_sec=round(start_ms / 1000, 3),
            end_sec=round(end_ms / 1000, 3),
            text=segment_text,
        )
    )


def _funasr_text_tokens(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if " " in stripped:
        return [token for token in stripped.split() if token]
    return [character for character in stripped if not character.isspace()]


def _timestamp_pair(timestamp: Any) -> tuple[float, float] | None:
    if not isinstance(timestamp, Sequence) or isinstance(timestamp, str) or len(timestamp) < 2:
        return None
    try:
        start_ms = float(timestamp[0])
        end_ms = float(timestamp[1])
    except (TypeError, ValueError):
        return None
    if end_ms < start_ms:
        return None
    return start_ms, end_ms


def _clean_funasr_text(text: str) -> str:
    return _join_funasr_tokens(_funasr_text_tokens(text))


def _join_funasr_tokens(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    if all(len(token) == 1 for token in tokens):
        return "".join(tokens).strip()
    return " ".join(tokens).strip()


_FUNASR_SENTENCE_BOUNDARY_TOKENS = {
    "。",
    "！",
    "？",
    "；",
    ".",
    "!",
    "?",
    ";",
}


def _funasr_model_ref(model_dir: Path, env_name: str, default_name: str) -> str:
    configured = os.environ.get(env_name)
    if configured:
        return configured
    named_child = model_dir / default_name
    if named_child.exists():
        return str(named_child)
    if default_name == "paraformer-zh":
        return str(model_dir)
    return default_name


def _optional_funasr_model_ref(
    model_dir: Path,
    env_name: str,
    default_name: str,
) -> str | None:
    configured = os.environ.get(env_name)
    if configured:
        normalized = configured.strip().lower()
        if normalized in {"0", "false", "none", "off", "disabled"}:
            return None
        return configured

    named_child = model_dir / default_name
    if named_child.exists() and (named_child / "model.pt").is_file():
        return str(named_child)
    return None


def _funasr_batch_size_seconds() -> int:
    configured = os.environ.get("FILE2DOC_FUNASR_BATCH_SIZE_SECONDS")
    if configured is None:
        return 300
    try:
        return max(1, int(configured))
    except ValueError:
        return 300


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
        stderr = error.stderr.strip()
        if _reports_missing_audio_stream(stderr):
            raise AudioParseFailure(
                "audio_track_absent",
                "Video source does not contain an audio track.",
            ) from error
        raise AudioParseFailure(
            "ffmpeg_failed",
            f"ffmpeg audio extraction failed: {stderr}",
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


def _reports_missing_audio_stream(stderr: str) -> bool:
    normalized = stderr.lower()
    return any(
        marker in normalized
        for marker in (
            "does not contain any stream",
            "matches no streams",
            "no audio stream",
        )
    )


def _coerce_transcript(result: Any) -> AudioTranscript:
    if isinstance(result, AudioTranscript):
        return result

    segments = tuple(_coerce_segment(segment) for segment in result.get("segments", ()))
    return AudioTranscript(
        text=str(result.get("text", "")).strip(),
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
