from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from file2doc.app import create_app
from file2doc.parsers import ParseOptions


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
    calls = []

    def fake_image_runner(source_path, options):
        calls.append((source_path.name, options.ocr_model))
        return """# Image Analysis

## Visible Text

HELLO 42

## Candidate Numeric Values

- 42

## Layout

Single centered label.

## Warnings

None.
"""

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                image_runner=fake_image_runner,
                ocr_model="fake-vision",
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
    assert calls == [(filename, "fake-vision")]

    content = client.get(job["result"]["content_url"]).text
    assert "## Visible Text" in content
    assert "HELLO 42" in content
    assert "## Candidate Numeric Values" in content
    assert "## Layout" in content
    assert "## Warnings" in content

    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["parser"]["name"] == "image-vision"
    source_media = next(item for item in manifest["media_index"] if item["kind"] == "source_image")
    assert source_media["media_type"] == expected_media_type

    source_artifact = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{source_media['artifact_id']}"
    )
    assert source_artifact.content == image_bytes
    assert source_artifact.headers["content-type"] == expected_media_type


def test_uploaded_jpeg_with_parser_warning_completes_with_warnings(tmp_path):
    def warning_image_runner(source_path, options):
        return """# Image Analysis

## Visible Text

LOW CONTRAST

## Candidate Numeric Values

None.

## Layout

One line of text.

## Warnings

- Image is low contrast; extracted text may be incomplete.
"""

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(image_runner=warning_image_runner),
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
            "message": "Image is low contrast; extracted text may be incomplete.",
        }
    ]
    assert manifest["media_index"][0]["media_type"] == "image/jpeg"


def test_uploaded_image_with_failing_vision_runner_fails_job(tmp_path):
    def failing_image_runner(source_path, options):
        raise TimeoutError("vision request timed out")

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(image_runner=failing_image_runner),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("timeout.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "image_vision_failed"
    assert "timed out" in job["error"]["message"]

    result_response = client.get(f"/parse-jobs/{job['job_id']}/result")
    assert result_response.status_code == 409
    assert result_response.json()["detail"]["code"] == "result_not_ready"


def test_uploaded_image_with_empty_vision_result_fails_job(tmp_path):
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(image_runner=lambda source_path, options: "   "),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("empty.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "empty_parse_result"


def _png_bytes() -> bytes:
    return _image_bytes("PNG")


def _jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


def _image_bytes(image_format: str) -> bytes:
    image = Image.new("RGB", (8, 8), color=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()
