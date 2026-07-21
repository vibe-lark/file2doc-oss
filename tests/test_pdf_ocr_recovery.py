from types import SimpleNamespace
import sys
import time
import types

import pytest

import file2doc.parsers as parser_module
from file2doc.parsers import ParseFailure, ParseOptions, parse_content_markdown


class EmptyMarkItDown:
    def convert(self, source_path):
        return SimpleNamespace(text_content="   ")


def test_empty_pdf_uses_configured_ocr_recovery(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    def fake_ocr_runner(source_path, options):
        assert source_path == source
        return "# Service Desk Contact\n\nCall 555-0100."

    parsed = parse_content_markdown(
        source,
        "application/pdf",
        ParseOptions(
            markitdown_factory=lambda: EmptyMarkItDown(),
            ocr_runner=fake_ocr_runner,
        ),
    )

    assert parsed.markdown == "# Service Desk Contact\n\nCall 555-0100.\n"
    assert parsed.diagnostics["name"] == "markitdown-ocr"
    assert parsed.diagnostics["ocr_used"] is True
    assert parsed.diagnostics["remote_services_used"] is True


def test_empty_pdf_without_ocr_configuration_returns_empty_result(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    parsed = parse_content_markdown(
        source,
        "application/pdf",
        ParseOptions(markitdown_factory=lambda: EmptyMarkItDown()),
    )

    assert parsed.markdown == ""
    assert parsed.diagnostics["name"] == "markitdown"
    assert parsed.diagnostics["empty_result"] is True


def test_empty_pdf_with_failing_ocr_reports_ocr_failure(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    def failing_ocr_runner(source_path, options):
        raise RuntimeError("remote unavailable")

    with pytest.raises(ParseFailure) as failure:
        parse_content_markdown(
            source,
            "application/pdf",
            ParseOptions(
                markitdown_factory=lambda: EmptyMarkItDown(),
                ocr_runner=failing_ocr_runner,
            ),
        )

    assert failure.value.code == "remote_ocr_failed"


def test_empty_pdf_with_empty_ocr_returns_empty_result(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    parsed = parse_content_markdown(
        source,
        "application/pdf",
        ParseOptions(
            markitdown_factory=lambda: EmptyMarkItDown(),
            ocr_runner=lambda source_path, options: "   ",
        ),
    )

    assert parsed.markdown == ""
    assert parsed.diagnostics["name"] == "markitdown-ocr"
    assert parsed.diagnostics["ocr_used"] is True
    assert parsed.diagnostics["remote_services_used"] is True
    assert parsed.diagnostics["empty_result"] is True


def test_remote_ocr_client_uses_configured_timeout(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

    class FakeMarkItDown:
        def __init__(self, **kwargs):
            captured["markitdown_kwargs"] = kwargs

        def convert(self, source_path):
            captured["source_path"] = source_path
            return SimpleNamespace(text_content="# OCR")

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setattr(parser_module, "MarkItDown", FakeMarkItDown)

    markdown = parser_module._run_markitdown_ocr(
        source,
        ParseOptions(
            ocr_model="doubao-vision",
            ocr_api_key="test-key",
            ocr_base_url="https://ocr.example.test/api/v3",
            ocr_timeout_seconds=15,
        ),
    )

    assert markdown == "# OCR"
    assert captured["client_kwargs"] == {
        "api_key": "test-key",
        "base_url": "https://ocr.example.test/api/v3",
        "timeout": 15,
    }
    assert captured["markitdown_kwargs"]["llm_client"].__class__ is FakeOpenAI
    assert captured["markitdown_kwargs"]["llm_model"] == "doubao-vision"
    assert captured["source_path"] == source


def test_remote_ocr_timeout_can_be_read_from_environment(monkeypatch):
    monkeypatch.setenv("FILE2DOC_OCR_MODEL", "doubao-vision")
    monkeypatch.setenv("FILE2DOC_OCR_API_KEY", "test-key")
    monkeypatch.setenv("FILE2DOC_OCR_TIMEOUT_SECONDS", "12")

    options = ParseOptions.from_env()

    assert options.ocr_timeout_seconds == 12


def test_remote_ocr_is_bounded_by_process_timeout(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    def hanging_remote_ocr(source_path, options):
        time.sleep(5)
        return "# Too late"

    monkeypatch.setattr(parser_module, "_run_direct_pdf_vision_ocr", hanging_remote_ocr)

    started_at = time.monotonic()
    with pytest.raises(ParseFailure) as failure:
        parse_content_markdown(
            source,
            "application/pdf",
            ParseOptions(
                markitdown_factory=lambda: EmptyMarkItDown(),
                ocr_model="doubao-vision",
                ocr_api_key="test-key",
                ocr_timeout_seconds=0.1,
            ),
        )

    assert time.monotonic() - started_at < 2
    assert failure.value.code == "remote_ocr_failed"
    assert "timed out" in str(failure.value)


def test_empty_pdf_uses_direct_page_vision_ocr_instead_of_markitdown_ocr(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    calls = []

    def fake_direct_ocr(source_path, options):
        calls.append((source_path, options.ocr_model))
        return "# Page OCR"

    def forbidden_markitdown_ocr(source_path, options):
        raise AssertionError("markitdown-ocr PDF converter should not be used")

    monkeypatch.setattr(parser_module, "_run_direct_pdf_vision_ocr_with_timeout", fake_direct_ocr)
    monkeypatch.setattr(parser_module, "_run_markitdown_ocr", forbidden_markitdown_ocr)

    parsed = parse_content_markdown(
        source,
        "application/pdf",
        ParseOptions(
            markitdown_factory=lambda: EmptyMarkItDown(),
            ocr_model="doubao-vision",
            ocr_api_key="test-key",
        ),
    )

    assert parsed.markdown == "# Page OCR\n"
    assert parsed.diagnostics["name"] == "direct-pdf-vision-ocr"
    assert calls == [(source, "doubao-vision")]
