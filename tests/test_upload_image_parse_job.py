from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from file2doc.app import create_app
from file2doc.parsers import ParseOptions


REAL_DISPLAY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "TBEVA-40-S7-display.jpg"
)


@pytest.mark.parametrize(
    ("filename", "content_type", "image_format", "expected_media_type"),
    [
        ("sample.png", "image/png", "PNG", "image/png"),
        ("sample.jpeg", "image/jpeg", "JPEG", "image/jpeg"),
        ("sample.jpg", "image/jpg", "JPEG", "image/jpeg"),
    ],
)
def test_uploaded_image_uses_vision_parser_and_exposes_source_image(
    tmp_path,
    filename,
    content_type,
    image_format,
    expected_media_type,
):
    image_bytes = _image_bytes(image_format)
    visual_client = _VisualClient(
        {
            "description": "A centered sample label.",
            "visibleText": ["HELLO 42"],
            "candidateNumericValues": ["42"],
            "layout": "Single centered label.",
            "imageProcessActions": ["Zoomed the centered label."],
            "imageProcessWarnings": [],
            "warnings": [],
        }
    )

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (filename, image_bytes, content_type)},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    assert len(visual_client.responses.calls) == 1

    content = client.get(job["result"]["content_url"]).text
    assert "## Visible Text" in content
    assert "HELLO 42" in content
    assert "## Candidate Numeric Values" in content
    assert "## Layout" in content
    assert "## Warnings" in content
    assert '"visibleText"' not in content

    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["parser"]["name"] == "file2doc-markitdown-visual"
    assert manifest["parser"]["markitdown_version"] == "0.1.2"
    assert manifest["parser"]["visual_plugin_version"] == "0.2.1"
    source_media = next(
        item for item in manifest["media_index"] if item["kind"] == "source_image"
    )
    assert source_media["media_type"] == expected_media_type

    source_artifact = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{source_media['artifact_id']}"
    )
    assert source_artifact.content == image_bytes
    assert source_artifact.headers["content-type"] == expected_media_type


def test_uploaded_instrument_photo_transcribes_only_illuminated_display_digits(
    tmp_path,
):
    """The visual contract must distinguish lit digits from dark display slots."""
    visual_client = _DisplayReadingResponsesClient()
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                REAL_DISPLAY_FIXTURE.name,
                REAL_DISPLAY_FIXTURE.read_bytes(),
                "image/jpeg",
            )
        },
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    content = client.get(job["result"]["content_url"]).text
    markdown_lines = set(content.splitlines())
    assert "- P 47.1" in markdown_lines
    assert "- 47.1" in markdown_lines
    assert "- P 847.1" not in markdown_lines
    assert "- 847.1" not in markdown_lines
    assert "- H 88.52" in markdown_lines


def test_uploaded_jpeg_with_parser_warning_completes_with_warnings(tmp_path):
    visual_client = _VisualClient(
        {
            "description": "A low-contrast label.",
            "visibleText": ["LOW CONTRAST"],
            "candidateNumericValues": [],
            "layout": "One line of text.",
            "imageProcessActions": ["Zoomed the low-contrast label."],
            "imageProcessWarnings": ["Zoom did not fully resolve the low contrast."],
            "warnings": ["Image is low contrast; extracted text may be incomplete."],
        }
    )

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("warning.jpg", _jpeg_bytes(), "image/jpeg")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed_with_warnings"
    assert job["warnings_count"] == 1

    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["warnings"] == [
        {
            "code": "image_parse_warning",
            "message": (
                "Zoom did not fully resolve the low contrast. "
                "Image is low contrast; extracted text may be incomplete."
            ),
        }
    ]
    assert manifest["media_index"][0]["media_type"] == "image/jpeg"


def test_uploaded_image_with_failing_vision_runner_fails_job(tmp_path):
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=_FailingVisualClient(
                    TimeoutError("vision request timed out")
                ),
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("timeout.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"
    assert "timeout.png" in job["error"]["message"]

    result_response = client.get(f"/parse-jobs/{job['job_id']}/result")
    assert result_response.status_code == 409
    assert result_response.json()["detail"]["code"] == "result_not_ready"


def test_uploaded_image_with_empty_vision_result_fails_job(tmp_path):
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=_RawVisualClient("   "),
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("empty.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"


def test_uploaded_image_with_invalid_structured_result_fails_without_fallback(tmp_path):
    visual_client = _RawVisualClient("not-json")
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("invalid.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"
    assert len(visual_client.responses.calls) == 1


def _png_bytes() -> bytes:
    return _image_bytes("PNG")


def _jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


def _image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (8, 8), color=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


class _CapturingResponses:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.payload))


class _VisualClient:
    def __init__(self, payload: dict) -> None:
        self.responses = _CapturingResponses(payload)


class _DisplayReadingResponsesClient:
    """Boundary simulator for the real Ark display-reading failure mode."""

    def __init__(self) -> None:
        self.responses = self
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["input"][0]["content"][1]["text"]
        verifies_active_segments = all(
            requirement in prompt.lower()
            for requirement in (
                "unilluminated segment outlines",
                "physical digit positions",
                "only the illuminated characters",
            )
        )
        reading = "P 47.1" if verifies_active_segments else "P 847.1"
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "description": "A transmittance and haze instrument display.",
                    "visibleText": [reading, "H 88.52"],
                    "candidateNumericValues": [
                        reading.removeprefix("P "),
                        "88.52",
                    ],
                    "layout": "P is left of H on the illuminated display.",
                    "imageProcessActions": [],
                    "imageProcessWarnings": [],
                    "warnings": [],
                }
            )
        )


class _RawResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class _RawVisualClient:
    def __init__(self, output_text: str) -> None:
        self.responses = _RawResponses(output_text)


class _FailingResponses:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **kwargs):
        raise self.error


class _FailingVisualClient:
    def __init__(self, error: Exception) -> None:
        self.responses = _FailingResponses(error)
