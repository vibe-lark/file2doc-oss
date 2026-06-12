from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_bearer_auth_blocks_requests_when_enabled(tmp_path):
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    unauthorized = client.get("/parse-jobs/job_missing")
    authorized = client.get(
        "/parse-jobs/job_missing",
        headers={"Authorization": "Bearer secret"},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["code"] == "unauthorized"
    assert authorized.status_code == 404
