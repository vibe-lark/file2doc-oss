from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_uploaded_text_file_produces_manifest_and_content_artifact(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    create_response = client.post(
        "/parse-jobs/upload",
        files={"file": ("hello.txt", b"# Hello\n\nUploaded through File2Doc.\n", "text/plain")},
        data={"parser_profile": "agent", "retention": "short"},
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["stage"] == "queued"
    assert created["percent"] == 0
    assert created["poll_url"] == f"/parse-jobs/{created['job_id']}"

    poll_response = client.get(created["poll_url"])
    assert poll_response.status_code == 200
    job = poll_response.json()
    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["percent"] == 100
    assert job["result"]["manifest_url"] == f"/parse-jobs/{created['job_id']}/result"

    manifest_response = client.get(job["result"]["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["schema_version"] == "file2doc.parse-result.v1"
    assert manifest["service_version"] == "0.1.23"
    assert manifest["skill"]["latest_version"] == "0.1.23"
    assert manifest["notices"][0]["code"] == "skill_version_unknown"
    assert manifest["source"]["filename"] == "hello.txt"
    assert manifest["content"]["artifact_id"] == job["result"]["content_artifact_id"]
    assert manifest["content"]["path"] == "content.md"

    content_response = client.get(job["result"]["content_url"])
    assert content_response.status_code == 200
    assert content_response.text == "# Hello\n\nUploaded through File2Doc.\n"


def test_upload_and_status_compare_installed_skill_version(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("hello.txt", b"hello agent", "text/plain")},
        headers={"X-File2Doc-Skill-Version": "0.1.22"},
    ).json()

    assert created["skill"]["update_available"] is True
    assert created["skill"]["update_required"] is False
    assert created["notices"][0]["code"] == "skill_update_available"

    job = client.get(
        f"/parse-jobs/{created['job_id']}",
        headers={"X-File2Doc-Skill-Version": "0.1.23"},
    ).json()
    assert job["skill"]["status"] == "current"
    assert job["notices"] == []
