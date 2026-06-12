from fastapi.testclient import TestClient

from file2doc.app import create_app
from fixtures import sample_file


def test_pdf_with_no_extracted_markdown_fails_visibly(tmp_path):
    sample = sample_file("blank-scan.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert job["error"]["code"] == "empty_parse_result"
    assert job["result"] is None

    manifest_response = client.get(f"/parse-jobs/{created['job_id']}/result")
    assert manifest_response.status_code == 409
    assert manifest_response.json()["detail"]["code"] == "result_not_ready"
