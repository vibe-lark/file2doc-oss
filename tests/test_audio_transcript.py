from pathlib import Path
import builtins

import pytest

from file2doc.audio import AudioParseFailure, AudioParseOptions, parse_audio_transcript


def test_unsupported_audio_content_type_fails_with_clear_code(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("hello", encoding="utf-8")

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(
            source,
            "text/plain",
            AudioParseOptions(model_dir=Path("/models/sherpa")),
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
    model_dir = tmp_path / "sherpa-model"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_bytes(b"model")
    (model_dir / "tokens.txt").write_text("tokens", encoding="utf-8")
    real_import = builtins.__import__

    def fail_sherpa_import(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sherpa_import)

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(source, "audio/wav", AudioParseOptions(model_dir=model_dir))

    assert failure.value.code == "local_asr_runtime_missing"
    assert "sherpa-onnx" in str(failure.value)


def test_injected_runner_returns_transcript_text_and_segments(tmp_path):
    source = tmp_path / "sample.wav"
    source.write_bytes(b"not a real wav")

    def fake_runner(source_path: Path, options: AudioParseOptions):
        assert source_path == source
        assert options.model_dir == Path("/models/sherpa")
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
        AudioParseOptions(model_dir=Path("/models/sherpa"), runner=fake_runner),
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


def test_media_file_extension_allows_octet_stream_uploads(tmp_path):
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"not a real mp4")

    parsed = parse_audio_transcript(
        source,
        "application/octet-stream",
        AudioParseOptions(
            model_dir=Path("/models/sherpa"),
            runner=lambda source_path, options: {"text": "extension based transcript"},
        ),
    )

    assert parsed.text == "extension based transcript"


def test_empty_local_asr_result_fails_with_clear_code(tmp_path):
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"not a real wav")

    with pytest.raises(AudioParseFailure) as failure:
        parse_audio_transcript(
            source,
            "audio/wav",
            AudioParseOptions(
                model_dir=Path("/models/sherpa"),
                runner=lambda source_path, options: {"text": "   "},
            ),
        )

    assert failure.value.code == "empty_transcript"
