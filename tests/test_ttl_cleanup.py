import time

from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_automatic_diagnostic_cleanup_does_not_expire_regular_jobs(tmp_path):
    app = create_app(
        storage_root=tmp_path,
        auth_enabled=False,
        diagnostic_cleanup_interval_seconds=0.01,
    )
    with TestClient(app) as client:
        created = client.post(
            "/parse-jobs/upload",
            files={"file": ("hello.txt", b"keep normal retention", "text/plain")},
            data={"parser_profile": "agent", "retention": "expired"},
        ).json()
        deadline = time.monotonic() + 1
        while True:
            job = client.get(created["poll_url"]).json()
            if job["status"] in {"completed", "completed_with_warnings"}:
                break
            assert job["status"] != "expired"
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.03)

        after_cleanup_interval = client.get(created["poll_url"]).json()

        assert after_cleanup_interval["status"] == "completed"
        content = client.get(after_cleanup_interval["result"]["content_url"])
        assert content.status_code == 200


def test_cleanup_expired_jobs_deletes_files_and_marks_job_expired(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    create_response = client.post(
        "/parse-jobs/upload",
        files={"file": ("expired.txt", b"expired content", "text/plain")},
        data={"parser_profile": "agent", "retention": "expired"},
    )
    created = create_response.json()
    job_id = created["job_id"]
    completed = client.get(created["poll_url"]).json()
    content_url = completed["result"]["content_url"]

    job_root = tmp_path / "jobs" / job_id
    assert (job_root / "source" / "expired.txt").exists()
    assert (job_root / "result-package" / "content.md").exists()

    cleanup_response = client.post("/admin/cleanup-expired")

    assert cleanup_response.status_code == 200
    summary = cleanup_response.json()
    assert summary["scanned_count"] == 1
    assert summary["expired_count"] == 1
    assert summary["deleted_jobs"] == [job_id]
    assert summary["deleted_bytes"] > 0
    assert not job_root.exists()

    job_response = client.get(created["poll_url"])
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "expired"
    assert job["stage"] == "expired"
    assert job["error"]["code"] == "job_expired"
    assert job["latest_progress"]["detail"]["code"] == "job_expired"

    result_response = client.get(completed["result"]["manifest_url"])
    artifact_response = client.get(content_url)
    package_response = client.get(completed["result"]["package_url"])

    assert result_response.status_code == 410
    assert result_response.json()["detail"]["code"] == "result_expired"
    assert artifact_response.status_code == 410
    assert artifact_response.json()["detail"]["code"] == "artifact_expired"
    assert package_response.status_code == 410
    assert package_response.json()["detail"]["code"] == "artifact_expired"


def test_cleanup_expired_requires_auth_when_auth_enabled(tmp_path):
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    unauthorized = client.post("/admin/cleanup-expired")
    authorized = client.post(
        "/admin/cleanup-expired",
        headers={"Authorization": "Bearer secret"},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["code"] == "unauthorized"
    assert authorized.status_code == 200
