import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from markitdown import StreamInfo
from PIL import Image

from file2doc.parsers import ParseOptions
from file2doc_markitdown_visual.embedded import EmbeddedVisualParser
from file2doc_markitdown_visual.plugin import VisualImageConverter
from file2doc_markitdown_visual.plugin import VisualExecutionPolicy


VALID_RESULT = {
    "description": "A test image.",
    "visibleText": [],
    "candidateNumericValues": [],
    "layout": "Centered.",
    "imageProcessActions": [],
    "imageProcessWarnings": [],
    "warnings": [],
}


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_visual_runtime_configuration_ignores_retired_ocr_aliases(monkeypatch):
    monkeypatch.setenv("FILE2DOC_OCR_MODEL", "retired-model")
    monkeypatch.setenv("FILE2DOC_OCR_API_KEY", "retired-key")
    monkeypatch.setenv("FILE2DOC_OCR_BASE_URL", "https://retired.example.test")
    monkeypatch.setenv("FILE2DOC_OCR_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("OPENAI_API_KEY", "retired-openai-key")
    monkeypatch.delenv("FILE2DOC_VISUAL_MODEL", raising=False)
    monkeypatch.delenv("FILE2DOC_VISUAL_API_KEY", raising=False)

    options = ParseOptions.from_env()

    assert options.visual_configured is False
    assert not hasattr(options, "ocr_model")
    assert not hasattr(options, "ocr_api_key")
    assert not hasattr(options, "ocr_base_url")
    assert not hasattr(options, "ocr_timeout_seconds")


def test_visual_runtime_reads_canonical_provider_and_execution_bounds(monkeypatch):
    monkeypatch.setenv("FILE2DOC_VISUAL_MODEL", "ep-visual")
    monkeypatch.setenv("FILE2DOC_VISUAL_API_KEY", "visual-key")
    monkeypatch.setenv("FILE2DOC_VISUAL_BASE_URL", "https://visual.example.test/api/v3")
    monkeypatch.setenv("FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS", "61")
    monkeypatch.setenv("FILE2DOC_VISUAL_MAX_CONCURRENCY", "3")

    options = ParseOptions.from_env()

    assert options.visual_model == "ep-visual"
    assert options.visual_api_key == "visual-key"
    assert options.visual_base_url == "https://visual.example.test/api/v3"
    assert options.visual_item_timeout_seconds == 17
    assert options.visual_job_deadline_seconds == 61
    assert options.visual_max_concurrency == 3


def test_visual_provider_calls_use_item_timeout_and_bounded_concurrency():
    lock = threading.Lock()
    active = 0
    max_active = 0
    timeouts = []

    class Responses:
        def create(self, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                timeouts.append(kwargs["timeout"])
            time.sleep(0.03)
            with lock:
                active -= 1
            return SimpleNamespace(output_text=json.dumps(VALID_RESULT))

    converter = VisualImageConverter(
        client=SimpleNamespace(responses=Responses()),
        model="ep-visual",
        item_timeout_seconds=17,
        job_deadline_seconds=60,
        max_concurrency=2,
    )
    stream_info = StreamInfo(filename="sample.png", mimetype="image/png")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: converter.convert(io.BytesIO(b"png"), stream_info),
                range(4),
            )
        )

    assert all("# Visual Analysis" in result.markdown for result in results)
    assert max_active == 2
    assert timeouts == [17, 17, 17, 17]


def test_job_deadline_preserves_completed_visuals_and_marks_remaining_unprocessed():
    class Responses:
        def create(self, **kwargs):
            time.sleep(0.06)
            return SimpleNamespace(output_text=json.dumps(VALID_RESULT))

    policy = VisualExecutionPolicy(
        item_timeout_seconds=10,
        job_deadline_seconds=0.05,
        max_concurrency=1,
    )
    parser = EmbeddedVisualParser(
        client=SimpleNamespace(responses=Responses()),
        model="ep-visual",
        execution_policy=policy,
    )
    session = parser.new_session()

    completed = session.parse(_png_bytes("white"), content_type="image/png")
    unprocessed = session.parse(_png_bytes("black"), content_type="image/png")

    assert "# Visual Analysis" in completed.markdown
    assert completed.warning is None
    assert unprocessed.markdown == ""
    assert "not processed" in (unprocessed.warning or "")
    assert "job deadline" in (unprocessed.warning or "")


def test_parse_entrypoint_passes_visual_execution_bounds_to_plugin(tmp_path):
    captured = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text=json.dumps(VALID_RESULT))

    source = tmp_path / "sample.png"
    source.write_bytes(_png_bytes("white"))

    from file2doc.parsers import parse_content_markdown

    parsed = parse_content_markdown(
        source,
        "image/png",
        ParseOptions(
            visual_client=SimpleNamespace(responses=Responses()),
            visual_model="ep-visual",
            visual_item_timeout_seconds=7,
            visual_job_deadline_seconds=20,
            visual_max_concurrency=1,
        ),
    )

    assert "# Visual Analysis" in parsed.markdown
    assert captured["timeout"] <= 7
    assert captured["timeout"] > 0
