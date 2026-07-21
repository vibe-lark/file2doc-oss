from fastapi.testclient import TestClient

from file2doc.app import create_app
from fixtures import sample_file


def test_pdf_with_no_extracted_markdown_returns_empty_result(tmp_path):
    sample = sample_file("Service Desk Contact information.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["error"] is None

    manifest_response = client.get(job["result"]["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["parser"]["empty_result"] is True

    content_response = client.get(job["result"]["content_url"])
    assert content_response.status_code == 200
    assert content_response.text == ""
