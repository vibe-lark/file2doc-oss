from fastapi.testclient import TestClient

from file2doc.app import create_app
from fixtures import sample_file


def test_uploaded_pdf_produces_non_empty_markdown_content(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    content = client.get(job["result"]["content_url"]).text
    assert "雅迪" in content
    assert len(content) > 1000
