from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from markitdown import MarkItDown, StreamInfo
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from file2doc_markitdown_visual import register_converters
from file2doc.app import create_app
from file2doc.parsers import ParseOptions


class _Responses:
    def __init__(self, payloads: list[dict | Exception]) -> None:
        self._payloads = iter(payloads)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = next(self._payloads)
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(output_text=json.dumps(payload))


class _VisualClient:
    def __init__(self, payloads: list[dict | Exception]) -> None:
        self.responses = _Responses(payloads)


class _DeadlineClient:
    def __init__(self) -> None:
        self.responses = self
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            call_index = len(self.calls)
            self.calls.append(kwargs)
        if call_index > 0:
            time.sleep(0.25)
        return SimpleNamespace(
            output_text=json.dumps(
                _visual_payload(f"Image {call_index + 1}", [str(call_index + 1)])
            )
        )


class _SlowClient:
    def __init__(self, delay: float) -> None:
        self.responses = self
        self.delay = delay

    def create(self, **kwargs):
        time.sleep(self.delay)
        return SimpleNamespace(
            output_text=json.dumps(_visual_payload("Too late", ["9.9"]))
        )


class _ConcurrencyClient:
    def __init__(self) -> None:
        self.responses = self
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def create(self, **kwargs):
        with self._lock:
            self.calls += 1
            call_index = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.04)
            return SimpleNamespace(
                output_text=json.dumps(
                    _visual_payload(f"Concurrent {call_index}", [str(call_index)])
                )
            )
        finally:
            with self._lock:
                self.active -= 1


def test_scanned_pdf_pages_use_the_public_visual_plugin_boundary():
    client = _VisualClient([_visual_payload("Scanned label", ["LOT 88.52"])])
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert_stream(
        io.BytesIO(_scanned_pdf_bytes("LOT 88.52")),
        stream_info=StreamInfo(filename="scan.pdf", mimetype="application/pdf"),
    )

    assert "# PDF Visual Parse Result" in result.markdown
    assert "## Page 1" in result.markdown
    assert "Scanned label" in result.markdown
    assert "LOT 88.52" in result.markdown
    assert len(client.responses.calls) == 1
    assert client.responses.calls[0]["tools"][0]["type"] == "image_process"


def test_mixed_pdf_preserves_native_text_and_inserts_visual_content_in_order():
    client = _VisualClient([_visual_payload("Embedded chart", ["H 88.52"])])
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert_stream(
        io.BytesIO(_mixed_pdf_bytes()),
        stream_info=StreamInfo(filename="mixed.pdf", mimetype="application/pdf"),
    )

    before = result.markdown.index("Before image")
    visual = result.markdown.index("Embedded chart")
    after = result.markdown.index("After image")
    assert before < visual < after
    assert "H 88.52" in result.markdown
    assert len(client.responses.calls) == 1


def test_failed_embedded_image_is_an_explicit_warning_without_losing_success():
    client = _VisualClient(
        [
            _visual_payload("First image", ["A 1.0"]),
            RuntimeError("provider unavailable"),
        ]
    )
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert_stream(
        io.BytesIO(_two_image_pdf_bytes()),
        stream_info=StreamInfo(filename="partial.pdf", mimetype="application/pdf"),
    )

    assert "First image" in result.markdown
    assert "## Warnings" in result.markdown
    assert "page 1 image 2" in result.markdown
    assert "provider unavailable" in result.markdown


def test_file2doc_reports_partial_pdf_as_completed_with_warnings(tmp_path):
    visual_client = _VisualClient(
        [
            _visual_payload("First image", ["A 1.0"]),
            RuntimeError("provider unavailable"),
        ]
    )
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="ep-visual",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("partial.pdf", _two_image_pdf_bytes(), "application/pdf")},
    ).json()
    job = _wait_for_job(client, created["poll_url"])

    assert job["status"] == "completed_with_warnings"
    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["warnings"][0]["code"] == "image_parse_warning"
    content = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{manifest['content']['artifact_id']}"
    ).text
    assert "First image" in content
    assert "page 1 image 2" in content


def test_file2doc_fails_when_scanned_pdf_has_no_usable_visual_result(tmp_path):
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=_VisualClient([RuntimeError("provider unavailable")]),
                visual_model="ep-visual",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("scan.pdf", _scanned_pdf_bytes("A 1.0"), "application/pdf")},
    ).json()
    job = _wait_for_job(client, created["poll_url"])

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"
    assert "no usable content" in job["error"]["message"]


def test_exact_duplicate_pdf_images_reuse_one_visual_result():
    client = _VisualClient([_visual_payload("Repeated logo", [])])
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert_stream(
        io.BytesIO(_duplicate_image_pdf_bytes()),
        stream_info=StreamInfo(filename="duplicates.pdf", mimetype="application/pdf"),
    )

    assert result.markdown.count("Repeated logo") == 2
    assert len(client.responses.calls) == 1


def test_job_deadline_preserves_completed_visuals_and_warns_for_remaining_items():
    client = _DeadlineClient()
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(
        markitdown,
        visual_client=client,
        visual_model="ep-visual",
        visual_item_timeout_seconds=1,
        visual_job_deadline_seconds=0.05,
        visual_max_concurrency=1,
    )

    started_at = time.monotonic()
    result = markitdown.convert_stream(
        io.BytesIO(_three_page_image_pdf_bytes()),
        stream_info=StreamInfo(filename="deadline.pdf", mimetype="application/pdf"),
    )

    assert time.monotonic() - started_at < 0.2
    assert "Image 1" in result.markdown
    assert "page 2 image 1" in result.markdown
    assert "page 3 image 1" in result.markdown
    assert "job deadline" in result.markdown.lower()
    assert all(call["timeout"] <= 0.05 for call in client.calls)


def test_file2doc_deadline_result_is_available_with_completed_page_content(tmp_path):
    visual_client = _DeadlineClient()
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="ep-visual",
                visual_item_timeout_seconds=1,
                visual_job_deadline_seconds=0.05,
                visual_max_concurrency=1,
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                "deadline.pdf",
                _three_page_image_pdf_bytes(),
                "application/pdf",
            )
        },
    ).json()
    job = _wait_for_job(client, created["poll_url"])

    assert job["status"] == "completed_with_warnings"
    manifest = client.get(job["result"]["manifest_url"]).json()
    content = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{manifest['content']['artifact_id']}"
    ).text
    assert "Image 1" in content
    assert "page 2 image 1" in content
    assert "page 3 image 1" in content


def test_visual_item_timeout_keeps_native_pdf_text_available():
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(
        markitdown,
        visual_client=_SlowClient(0.25),
        visual_model="ep-visual",
        visual_item_timeout_seconds=0.05,
        visual_job_deadline_seconds=1,
        visual_max_concurrency=1,
    )

    started_at = time.monotonic()
    result = markitdown.convert_stream(
        io.BytesIO(_mixed_pdf_bytes()),
        stream_info=StreamInfo(filename="timeout.pdf", mimetype="application/pdf"),
    )

    assert time.monotonic() - started_at < 0.2
    assert "Before image" in result.markdown
    assert "After image" in result.markdown
    assert "visual item timed out" in result.markdown


def test_recoverable_pdf_with_truncated_eof_still_preserves_mixed_content():
    client = _VisualClient([_visual_payload("Recovered image", ["H 88.52"])])
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert_stream(
        io.BytesIO(_mixed_pdf_bytes()[:-20]),
        stream_info=StreamInfo(filename="recoverable.pdf", mimetype="application/pdf"),
    )

    assert "Before image" in result.markdown
    assert "Recovered image" in result.markdown
    assert "After image" in result.markdown
    assert "PDF structure required page-render recovery" in result.markdown


def test_pdf_visual_calls_respect_bounded_concurrency():
    client = _ConcurrencyClient()
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(
        markitdown,
        visual_client=client,
        visual_model="ep-visual",
        visual_item_timeout_seconds=1,
        visual_job_deadline_seconds=1,
        visual_max_concurrency=2,
    )

    result = markitdown.convert_stream(
        io.BytesIO(_three_image_pdf_bytes()),
        stream_info=StreamInfo(filename="concurrency.pdf", mimetype="application/pdf"),
    )

    assert "Concurrent" in result.markdown
    assert client.calls == 3
    assert client.max_active == 2


def _visual_payload(description: str, visible_text: list[str]) -> dict:
    return {
        "description": description,
        "visibleText": visible_text,
        "candidateNumericValues": [
            value
            for text in visible_text
            for value in text.split()
            if any(character.isdigit() for character in value)
        ],
        "layout": "Top to bottom.",
        "warnings": [],
    }


def _scanned_pdf_bytes(text: str) -> bytes:
    image = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(image).text((24, 72), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=144)
    return buffer.getvalue()


def _mixed_pdf_bytes() -> bytes:
    embedded = io.BytesIO()
    image = Image.new("RGB", (120, 50), "white")
    ImageDraw.Draw(image).text((10, 18), "H 88.52", fill="black")
    image.save(embedded, format="PNG")
    embedded.seek(0)

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(320, 480))
    document.drawString(30, 430, "Before image")
    document.drawImage(ImageReader(embedded), 30, 240, width=240, height=100)
    document.drawString(30, 120, "After image")
    document.save()
    return buffer.getvalue()


def _two_image_pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(320, 480))
    document.drawImage(ImageReader(_png_stream("A 1.0")), 30, 300, 240, 80)
    document.drawImage(ImageReader(_png_stream("B 2.0")), 30, 140, 240, 80)
    document.save()
    return buffer.getvalue()


def _duplicate_image_pdf_bytes() -> bytes:
    image = ImageReader(_png_stream("SAME"))
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(320, 480))
    document.drawImage(image, 30, 300, 240, 80)
    document.drawImage(image, 30, 140, 240, 80)
    document.save()
    return buffer.getvalue()


def _three_image_pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(320, 600))
    for index, y in enumerate((440, 280, 120), 1):
        document.drawImage(ImageReader(_png_stream(str(index))), 30, y, 240, 80)
    document.save()
    return buffer.getvalue()


def _three_page_image_pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(320, 480))
    for index in range(1, 4):
        document.drawImage(ImageReader(_png_stream(str(index))), 30, 200, 240, 80)
        document.showPage()
    document.save()
    return buffer.getvalue()


def _png_stream(text: str) -> io.BytesIO:
    buffer = io.BytesIO()
    image = Image.new("RGB", (120, 40), "white")
    ImageDraw.Draw(image).text((8, 14), text, fill="black")
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _wait_for_job(client: TestClient, poll_url: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(poll_url).json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("parse job did not finish")
