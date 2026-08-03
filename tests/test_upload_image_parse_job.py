from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import base64
from email.message import Message
import time
import urllib.error
from zipfile import ZipFile

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
    assert "### Provider Tool Calls\n\nNone." in content

    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["parser"]["name"] == "file2doc-markitdown-visual"
    assert manifest["parser"]["markitdown_version"] == "0.1.2"
    assert manifest["parser"]["visual_plugin_version"] == "0.3.3"
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
    assert zoom_media["diagnostic_ref"] == (
        zoom_media["source_ref"] + ":image_process:0001"
    )
    assert zoom_media["image_process"] == {
        "action": "zoom",
        "arguments": {
            "image_index": "0",
            "bbox_str": "[10, 20, 90, 80]",
            "scale": "2",
        },
        "status": "completed",
        "result": {
            "diagnostic_ref": zoom_media["diagnostic_ref"],
            "artifact_id": zoom_media["artifact_id"],
        },
        "warnings": [],
    }
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
    assert f"derived artifact `{zoom_media['diagnostic_ref']}`" in content


def test_releasing_diagnostics_shortens_only_derived_artifact_lifetime(tmp_path):
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A zoomed label.",
            "visibleText": ["42"],
            "candidateNumericValues": ["42"],
            "layout": "Centered.",
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


def test_released_diagnostic_cannot_be_downloaded_through_result_package(tmp_path):
    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            parse_options=ParseOptions(
                visual_client=_diagnostic_visual_client(),
                visual_model="fake-vision",
                visual_artifact_ttl_seconds=3600,
                visual_artifact_release_grace_seconds=0.01,
            ),
        )
    )
    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("display.png", _png_bytes(), "image/png")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()
    manifest = client.get(job["result"]["manifest_url"]).json()
    diagnostic = next(
        item
        for item in manifest["media_index"]
        if item.get("attachment_role") == "diagnostic_only"
    )
    source = next(
        item for item in manifest["media_index"] if item["kind"] == "source_image"
    )

    assert client.post(
        f"/parse-jobs/{job['job_id']}/diagnostics/release"
    ).status_code == 200
    time.sleep(0.03)

    package_response = client.get(job["result"]["package_url"])

    assert package_response.status_code == 200
    with ZipFile(io.BytesIO(package_response.content)) as package:
        assert diagnostic["path"] not in package.namelist()
        assert package.read("content.md")
    expired = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{diagnostic['artifact_id']}"
    )
    assert expired.status_code == 410
    assert expired.json()["detail"]["code"] == "artifact_expired"
    assert client.get(job["result"]["content_url"]).status_code == 200
    assert client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{source['artifact_id']}"
    ).status_code == 200


def test_runtime_automatically_tombstones_expired_diagnostics(tmp_path):
    app = create_app(
        storage_root=tmp_path,
        auth_enabled=False,
        parse_options=ParseOptions(
            visual_client=_diagnostic_visual_client(),
            visual_model="fake-vision",
            visual_artifact_ttl_seconds=3600,
            visual_artifact_release_grace_seconds=0.01,
        ),
        diagnostic_cleanup_interval_seconds=0.01,
    )
    with TestClient(app) as client:
        created = client.post(
            "/parse-jobs/upload",
            files={"file": ("display.png", _png_bytes(), "image/png")},
            data={"parser_profile": "agent", "retention": "short"},
        ).json()
        deadline = time.monotonic() + 1
        while True:
            job = client.get(created["poll_url"]).json()
            if job["status"] in {"completed", "completed_with_warnings"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        manifest = client.get(job["result"]["manifest_url"]).json()
        diagnostic = next(
            item
            for item in manifest["media_index"]
            if item.get("attachment_role") == "diagnostic_only"
        )
        source = next(
            item for item in manifest["media_index"] if item["kind"] == "source_image"
        )
        assert client.post(
            f"/parse-jobs/{job['job_id']}/diagnostics/release"
        ).status_code == 200

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            current_manifest = client.get(job["result"]["manifest_url"]).json()
            current_diagnostic = next(
                item
                for item in current_manifest["media_index"]
                if item["artifact_id"] == diagnostic["artifact_id"]
            )
            if current_diagnostic.get("availability") == "expired":
                break
            time.sleep(0.01)

        assert current_diagnostic["availability"] == "expired"
        assert current_diagnostic["expired_at"]
        assert not (
            tmp_path
            / "jobs"
            / job["job_id"]
            / "result-package"
            / diagnostic["path"]
        ).exists()
        assert client.get(
            f"/parse-jobs/{job['job_id']}/artifacts/{diagnostic['artifact_id']}"
        ).status_code == 410
        package_response = client.get(job["result"]["package_url"])
        with ZipFile(io.BytesIO(package_response.content)) as package:
            assert diagnostic["path"] not in package.namelist()
            assert package.read("content.md")
        assert client.get(job["result"]["content_url"]).status_code == 200
        assert client.get(
            f"/parse-jobs/{job['job_id']}/artifacts/{source['artifact_id']}"
        ).status_code == 200


def test_expired_diagnostic_get_updates_public_tombstone_before_scheduled_cleanup(
    tmp_path,
):
    app = create_app(
        storage_root=tmp_path,
        auth_enabled=False,
        parse_options=ParseOptions(
            visual_client=_diagnostic_visual_client(),
            visual_model="fake-vision",
            visual_artifact_ttl_seconds=0.01,
        ),
        diagnostic_cleanup_interval_seconds=60,
    )
    with TestClient(app) as client:
        created = client.post(
            "/parse-jobs/upload",
            files={"file": ("display.png", _png_bytes(), "image/png")},
            data={"parser_profile": "agent", "retention": "short"},
        ).json()
        deadline = time.monotonic() + 1
        while True:
            job = client.get(created["poll_url"]).json()
            if job["status"] in {"completed", "completed_with_warnings"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        manifest = client.get(job["result"]["manifest_url"]).json()
        diagnostic = next(
            item
            for item in manifest["media_index"]
            if item.get("attachment_role") == "diagnostic_only"
        )
        time.sleep(0.03)

        expired = client.get(
            f"/parse-jobs/{job['job_id']}/artifacts/{diagnostic['artifact_id']}"
        )

        assert expired.status_code == 410
        after = client.get(job["result"]["manifest_url"]).json()
        tombstone = next(
            item
            for item in after["media_index"]
            if item["artifact_id"] == diagnostic["artifact_id"]
        )
        assert tombstone["availability"] == "expired"
        assert tombstone["expired_at"]
        assert not (
            tmp_path
            / "jobs"
            / job["job_id"]
            / "result-package"
            / diagnostic["path"]
        ).exists()


def test_cleanup_expires_only_visual_diagnostics_and_keeps_tombstone(tmp_path):
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A rotated label.",
            "visibleText": ["LOT 42"],
            "candidateNumericValues": ["42"],
            "layout": "Landscape after rotation.",
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
                visual_artifact_ttl_seconds=0.5,
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

    time.sleep(0.55)
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


def test_image_process_result_rejects_private_network_url_before_download(
    tmp_path, monkeypatch
):
    visual_client = _diagnostic_visual_client()
    visual_client.result_url = (
        "https://ark-ams-storage-cn-beijing.tos-cn-beijing.volces.com/private.png"
    )
    network_called = False

    def forbidden_network_call(*args, **kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("unsafe provider URL reached the network")

    monkeypatch.setattr("urllib.request.build_opener", forbidden_network_call)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    job = _upload_with_visual_client(tmp_path, visual_client)

    assert job["status"] == "failed"
    assert "provider image host resolves to a non-public address" in job["error"]["message"]
    assert network_called is False


def test_image_process_result_rejects_unlisted_hostname_before_download(
    tmp_path, monkeypatch
):
    visual_client = _diagnostic_visual_client()
    visual_client.result_url = "https://untrusted.example.test/zoom.png"
    network_called = False

    def forbidden_network_call(*args, **kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("unlisted provider host reached the network")

    monkeypatch.setattr("urllib.request.build_opener", forbidden_network_call)
    job = _upload_with_visual_client(tmp_path, visual_client)

    assert job["status"] == "failed"
    assert "provider image host is not in the allowlist" in job["error"]["message"]
    assert network_called is False


def test_image_process_result_does_not_follow_redirects(tmp_path, monkeypatch):
    visual_client = _diagnostic_visual_client()
    visual_client.result_url = (
        "https://ark-ams-storage-cn-beijing.tos-cn-beijing.volces.com/zoom.png"
    )
    redirect_headers = Message()
    redirect_headers["Location"] = "https://127.0.0.1/private.png"

    def redirect_response(*args, **kwargs):
        raise urllib.error.HTTPError(
            visual_client.result_url,
            302,
            "Found",
            redirect_headers,
            None,
        )

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *handlers: SimpleNamespace(open=redirect_response),
    )
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )
    job = _upload_with_visual_client(tmp_path, visual_client)

    assert job["status"] == "failed"
    assert "provider image redirects are not allowed" in job["error"]["message"]


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
    visual_client = _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A low-contrast label.",
            "visibleText": ["LOW CONTRAST"],
            "candidateNumericValues": [],
            "layout": "One line of text.",
            "warnings": ["Image is low contrast; extracted text may be incomplete."],
        },
        action="zoom",
        result_bytes=_png_bytes(),
        warnings=["Zoom did not fully resolve the low contrast."],
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


def test_model_cannot_self_report_image_process_actions(tmp_path):
    visual_client = _VisualClient(
        {
            "description": "A label.",
            "visibleText": ["42"],
            "candidateNumericValues": ["42"],
            "layout": "Centered.",
            "warnings": [],
            "imageProcessActions": ["I used Zoom"],
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
        files={"file": ("forged.png", _png_bytes(), "image/png")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "visual_item_failed"
    assert "required schema" in job["error"]["message"]


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
    def __init__(
        self,
        *,
        payload: dict,
        action: str,
        result_bytes: bytes,
        warnings: list[str] | None = None,
    ) -> None:
        encoded = base64.b64encode(result_bytes).decode("ascii")
        self.responses = self
        self.payload = payload
        self.action = action
        self.result_url = f"data:image/png;base64,{encoded}"
        self.warnings = warnings or []
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(self.payload),
            output=[
                SimpleNamespace(type="message", status="completed"),
                SimpleNamespace(
                    type="image_process",
                    action=SimpleNamespace(
                        type=self.action,
                        result_image_url=self.result_url,
                    ),
                    arguments=SimpleNamespace(
                        image_index=0,
                        bbox_str="[10, 20, 90, 80]",
                        scale=2,
                    ),
                    status="completed",
                    warnings=self.warnings,
                )
            ],
        )


def _diagnostic_visual_client() -> _VisualClientWithImageProcessArtifact:
    return _VisualClientWithImageProcessArtifact(
        payload={
            "description": "A diagnostic image.",
            "visibleText": ["42"],
            "candidateNumericValues": ["42"],
            "layout": "Centered.",
            "warnings": [],
        },
        action="zoom",
        result_bytes=_png_bytes(),
    )


def _upload_with_visual_client(tmp_path, visual_client) -> dict:
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
    return client.get(created["poll_url"]).json()


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
