from fastapi.testclient import TestClient

from file2doc.app import create_app
from fixtures import sample_file


def test_successful_parse_manifest_exposes_parser_diagnostics(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
    ).json()
    job = client.get(created["poll_url"]).json()

    response = client.get(job["result"]["manifest_url"])

    assert response.status_code == 200
    parser = response.json()["parser"]
    assert parser["name"] == "markitdown"
    assert isinstance(parser["version"], str)
    assert parser["version"]
    assert isinstance(parser["elapsed_ms"], int | float)
    assert parser["elapsed_ms"] >= 0
    assert parser["ocr_used"] is False
    assert parser["remote_services_used"] is False
