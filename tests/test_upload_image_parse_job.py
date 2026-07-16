from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import base64
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from file2doc.app import create_app
from file2doc.parsers import ParseOptions


REAL_DISPLAY_FIXTURE = Path(__file__).parent / "fixtures" / "TBEVA-40-S7-display.jpg"


def test_visual_runtime_logs_provider_and_model_without_sensitive_payload(
    tmp_path,
    caplog,
):
    secret = "visual-api-key-must-not-be-logged"
    payload_marker = "VISIBLE-PAYLOAD-MUST-NOT-BE-LOGGED"
    visual_client = _VisualClient(
        {
            "description": payload_marker,
            "visibleText": [],
            "candidateNumericValues": [],
            "layout": "Single centered label.",
            "imageProcessActions": [],
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
                visual_api_key=secret,
            ),
        )
    )
    caplog.set_level(logging.INFO, logger="file2doc.visual")

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("sample.png", _image_bytes("PNG"), "image/png")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "provider=ark-responses" in messages
    assert "model=fake-vision" in messages
    assert secret not in messages
    assert payload_marker not in messages


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
    assert "## Visual Parsing Runtime" in content
    assert "- Provider: ark-responses" in content
    assert "- Model: fake-vision" in content
    assert '"visibleText"' not in content

    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["parser"]["name"] == "file2doc-markitdown-visual"
    assert manifest["parser"]["markitdown_version"] == "0.1.2"
    assert manifest["parser"]["visual_plugin_version"] == "0.2.1"
    source_media = next(
        item for item in manifest["media_index"] if item["kind"] == "source_image"
    )
    assert not any(
        item["kind"].startswith("image_process_")
        for item in manifest["media_index"]
    )
    assert source_media["media_type"] == expected_media_type

    source_artifact = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{source_media['artifact_id']}"
    )
    assert source_artifact.content == image_bytes
    assert source_artifact.headers["content-type"] == expected_media_type


def test_zoom_result_is_exposed_as_short_lived_diagnostic_artifact(tmp_path):
    image_bytes = _png_bytes()
    zoom_bytes = _image_bytes("PNG")
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A zoomed laboratory display.",
            "visibleText": ["H 88.52"],
            "candidateNumericValues": ["88.52"],
            "layout": "One illuminated display.",
            "imageProcessActions": ["Zoom"],
            "imageProcessWarnings": [],
            "warnings": [],
        },
        action="zoom",
        result_bytes=zoom_bytes,
    )
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
                visual_artifact_ttl_seconds=3600,
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("display.png", image_bytes, "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    manifest = client.get(job["result"]["manifest_url"]).json()
    zoom_media = next(
        item
        for item in manifest["media_index"]
        if item["kind"] == "image_process_zoom_result"
    )
    assert zoom_media["source_ref"].startswith("source_image_sha256:")
    assert zoom_media["lifecycle"] == "structured_workflow_draft"
    assert zoom_media["attachment_role"] == "diagnostic_only"
    assert zoom_media["expires_at"] < manifest["expires_at"]

    response = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{zoom_media['artifact_id']}"
    )
    assert response.status_code == 200
    assert response.content == zoom_bytes
    assert response.headers["content-type"] == "image/png"

    content = client.get(job["result"]["content_url"]).text
    assert zoom_media["path"] not in content


def test_releasing_diagnostics_shortens_only_derived_artifact_lifetime(tmp_path):
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A zoomed label.",
            "visibleText": ["42"],
            "candidateNumericValues": ["42"],
            "layout": "Centered.",
            "imageProcessActions": ["Zoom"],
            "imageProcessWarnings": [],
            "warnings": [],
        },
        action="zoom",
        result_bytes=_png_bytes(),
    )
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
                visual_artifact_ttl_seconds=3600,
                visual_artifact_release_grace_seconds=60,
            ),
        )
    )
    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("label.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()
    before = client.get(job["result"]["manifest_url"]).json()
    diagnostic_before = next(
        item
        for item in before["media_index"]
        if item["kind"] == "image_process_zoom_result"
    )
    source_before = next(
        item for item in before["media_index"] if item["kind"] == "source_image"
    )

    first = client.post(
        f"/parse-jobs/{job['job_id']}/diagnostics/release"
    )
    second = client.post(
        f"/parse-jobs/{job['job_id']}/diagnostics/release"
    )

    assert first.status_code == 200
    assert first.json() == {
        "job_id": job["job_id"],
        "released_count": 1,
        "artifact_ids": [diagnostic_before["artifact_id"]],
        "release_expires_at": first.json()["release_expires_at"],
        "already_released": False,
    }
    assert first.json()["release_expires_at"] < diagnostic_before["expires_at"]
    assert second.json()["already_released"] is True
    assert second.json()["release_expires_at"] == first.json()["release_expires_at"]

    after = client.get(job["result"]["manifest_url"]).json()
    diagnostic_after = next(
        item
        for item in after["media_index"]
        if item["artifact_id"] == diagnostic_before["artifact_id"]
    )
    source_after = next(
        item for item in after["media_index"] if item["kind"] == "source_image"
    )
    assert diagnostic_after["expires_at"] == first.json()["release_expires_at"]
    assert diagnostic_after["release_requested_at"]
    assert source_after == source_before
    assert client.get(job["result"]["content_url"]).status_code == 200


def test_cleanup_expires_only_visual_diagnostics_and_keeps_tombstone(tmp_path):
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A rotated label.",
            "visibleText": ["LOT 42"],
            "candidateNumericValues": ["42"],
            "layout": "Landscape after rotation.",
            "imageProcessActions": ["Rotate"],
            "imageProcessWarnings": [],
            "warnings": [],
        },
        action="rotate",
        result_bytes=_png_bytes(),
    )
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=visual_client,
                visual_model="fake-vision",
                visual_artifact_ttl_seconds=0.05,
            ),
        )
    )
    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("rotated.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()
    manifest = client.get(job["result"]["manifest_url"]).json()
    diagnostic = next(
        item
        for item in manifest["media_index"]
        if item["kind"] == "image_process_rotate_result"
    )
    diagnostic_url = (
        f"/parse-jobs/{job['job_id']}/artifacts/{diagnostic['artifact_id']}"
    )
    assert client.get(diagnostic_url).status_code == 200

    time.sleep(0.08)
    cleanup = client.post("/admin/cleanup-expired")

    assert cleanup.status_code == 200
    assert cleanup.json()["expired_artifact_count"] == 1
    assert cleanup.json()["expired_artifact_ids"] == [diagnostic["artifact_id"]]
    expired = client.get(diagnostic_url)
    assert expired.status_code == 410
    assert expired.json()["detail"]["code"] == "artifact_expired"

    after = client.get(job["result"]["manifest_url"]).json()
    tombstone = next(
        item
        for item in after["media_index"]
        if item["artifact_id"] == diagnostic["artifact_id"]
    )
    assert tombstone["availability"] == "expired"
    assert tombstone["expired_at"]
    assert client.get(job["result"]["content_url"]).status_code == 200
    source = next(
        item for item in after["media_index"] if item["kind"] == "source_image"
    )
    assert client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{source['artifact_id']}"
    ).status_code == 200


def test_unavailable_image_process_result_fails_without_markdown_fallback(tmp_path):
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "This must not be accepted without its Zoom evidence.",
            "visibleText": ["42"],
            "candidateNumericValues": ["42"],
            "layout": "Centered.",
            "imageProcessActions": ["Zoom"],
            "imageProcessWarnings": [],
            "warnings": [],
        },
        action="zoom",
        result_bytes=_png_bytes(),
    )
    visual_client.result_url = "http://provider.example.test/zoom.png"
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
        files={"file": ("display.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"
    assert "Image Process zoom result could not be retained" in job["error"]["message"]
    assert client.get(f"/parse-jobs/{job['job_id']}/result").status_code == 409


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


class _VisualClientWithImageProcessArtifact:
    def __init__(self, *, payload: dict, action: str, result_bytes: bytes) -> None:
        encoded = base64.b64encode(result_bytes).decode("ascii")
        self.responses = self
        self.payload = payload
        self.action = action
        self.result_url = f"data:image/png;base64,{encoded}"
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(self.payload),
            output=[
                SimpleNamespace(
                    type="image_process",
                    action=SimpleNamespace(
                        type=self.action,
                        result_image_url=self.result_url,
                    ),
                    arguments=SimpleNamespace(image_index=0),
                )
            ],
        )


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
