from fastapi.testclient import TestClient

from file2doc.app import create_app
from fixtures import sample_file


def test_uploaded_pdf_manifest_includes_downloadable_not_configured_ocr_sidecar(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    manifest = client.get(job["result"]["manifest_url"]).json()

    first_page = manifest["page_index"][0]
    assert first_page["page"] == 1
    assert first_page["ocr_path"] == "ocr/page_001.json"
    assert first_page["ocr_artifact_id"]

    artifacts_by_id = {artifact["artifact_id"]: artifact for artifact in manifest["artifacts"]}
    ocr_artifact = artifacts_by_id[first_page["ocr_artifact_id"]]
    assert ocr_artifact == {
        "artifact_id": first_page["ocr_artifact_id"],
        "kind": "ocr_sidecar",
        "path": "ocr/page_001.json",
        "media_type": "application/json; charset=utf-8",
        "page": 1,
    }

    response = client.get(
        f"/parse-jobs/{job['job_id']}/artifacts/{first_page['ocr_artifact_id']}"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {
        "page": 1,
        "status": "not_configured",
        "engine": None,
        "text": "",
        "blocks": [],
        "warnings": ["OCR is not configured for this deployment."],
    }
