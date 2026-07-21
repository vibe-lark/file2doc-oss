from pathlib import Path
import builtins

import pytest

from file2doc.audio import (
    AudioParseFailure,
    AudioParseOptions,
    _optional_funasr_model_ref,
    parse_audio_transcript,
)


def test_unsupported_audio_content_type_fails_with_clear_code(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("hello", encoding="utf-8")

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(
            source,
            "text/plain",
            AudioParseOptions(model_dir=Path("/models/funasr")),
        )

    assert failure.value.code == "unsupported_audio_content_type"


def test_missing_local_asr_config_fails_with_clear_code(tmp_path):
    source = tmp_path / "sample.wav"
    source.write_bytes(b"not a real wav")

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(source, "audio/wav", AudioParseOptions(model_dir=None))

    assert failure.value.code == "local_asr_not_configured"
    assert "local ASR model" in str(failure.value)


def test_missing_local_asr_runtime_fails_with_clear_code(tmp_path, monkeypatch):
    source = tmp_path / "sample.wav"
    source.write_bytes(b"not a real wav")
    model_dir = tmp_path / "funasr-model"
    model_dir.mkdir()
    real_import = builtins.__import__

    def fail_funasr_import(name, *args, **kwargs):
        if name == "funasr":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_funasr_import)

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(source, "audio/wav", AudioParseOptions(model_dir=model_dir))

    assert failure.value.code == "local_asr_runtime_missing"
    assert "funasr" in str(failure.value).lower()


def test_injected_runner_returns_transcript_text_and_segments(tmp_path):
    source = tmp_path / "sample.wav"
    source.write_bytes(b"not a real wav")

    def fake_runner(source_path: Path, options: AudioParseOptions):
        assert source_path == source
        assert options.model_dir == Path("/models/funasr")
        return {
            "text": "hello world",
            "segments": [
                {"start_sec": 0.0, "end_sec": 1.2, "text": "hello"},
                {"start_sec": 1.2, "end_sec": 2.4, "text": "world"},
            ],
            "engine": "fake-local-asr",
            "time_alignment_method": "vad_chunking",
        }

    parsed = parse_audio_transcript(
        source,
        "audio/wav",
        AudioParseOptions(model_dir=Path("/models/funasr"), runner=fake_runner),
    )

    assert parsed.text == "hello world"
    assert parsed.time_aligned is True
    assert parsed.engine == "fake-local-asr"
    assert parsed.segments[0].start_sec == 0.0
    assert parsed.segments[1].text == "world"
    assert parsed.to_manifest_transcript() == {
        "text": "hello world",
        "time_aligned": True,
        "time_alignment_method": "vad_chunking",
        "engine": "fake-local-asr",
        "segments": [
            {"id": "seg-1", "start_sec": 0.0, "end_sec": 1.2, "text": "hello"},
            {"id": "seg-2", "start_sec": 1.2, "end_sec": 2.4, "text": "world"},
        ],
    }


def test_funasr_timestamp_output_falls_back_to_time_aligned_segments():
    from file2doc.audio import _segments_from_funasr_timestamps

    segments = _segments_from_funasr_timestamps(
        "把 糖 浆 倒 入 杯 中 加 冰 摇 匀 出 杯",
        [
            [0, 500],
            [500, 900],
            [900, 1300],
            [1300, 1800],
            [1800, 2300],
            [2300, 2800],
            [2800, 3300],
            [3300, 3800],
            [3800, 4300],
            [4300, 4800],
            [4800, 5300],
            [5300, 5800],
            [5800, 6300],
        ],
    )

    assert len(segments) == 1
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == 6.3
    assert segments[0].text == "把糖浆倒入杯中加冰摇匀出杯"


def test_media_file_extension_allows_octet_stream_uploads(tmp_path):
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"not a real mp4")

    parsed = parse_audio_transcript(
        source,
        "application/octet-stream",
        AudioParseOptions(
            model_dir=Path("/models/funasr"),
            runner=lambda source_path, options: {"text": "extension based transcript"},
        ),
    )

    assert parsed.text == "extension based transcript"


def test_empty_local_asr_result_returns_empty_transcript(tmp_path):
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"not a real wav")

    parsed = parse_audio_transcript(
        source,
        "audio/wav",
        AudioParseOptions(
            model_dir=Path("/models/funasr"),
            runner=lambda source_path, options: {"text": "   "},
        ),
    )

    assert parsed.text == ""
    assert parsed.segments == ()
    assert parsed.diagnostics["empty_result"] is True


def test_optional_funasr_punc_model_only_uses_present_local_model(tmp_path, monkeypatch):
    model_dir = tmp_path / "funasr"
    punc_dir = model_dir / "ct-punc"
    punc_dir.mkdir(parents=True)

    assert _optional_funasr_model_ref(model_dir, "FILE2DOC_FUNASR_PUNC_MODEL", "ct-punc") is None

    (punc_dir / "model.pt").write_bytes(b"fake punc model")

    assert (
        _optional_funasr_model_ref(model_dir, "FILE2DOC_FUNASR_PUNC_MODEL", "ct-punc")
        == str(punc_dir)
    )

    monkeypatch.setenv("FILE2DOC_FUNASR_PUNC_MODEL", "disabled")

    assert _optional_funasr_model_ref(model_dir, "FILE2DOC_FUNASR_PUNC_MODEL", "ct-punc") is None
