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


def test_api_documentation_requires_bearer_auth_when_auth_is_enabled(tmp_path):
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    docs_response = client.get("/docs")
    openapi_response = client.get("/openapi.json")
    redoc_response = client.get("/redoc")
    authorized_docs_response = client.get(
        "/docs",
        headers={"Authorization": "Bearer secret"},
    )
    authorized_openapi_response = client.get(
        "/openapi.json",
        headers={"Authorization": "Bearer secret"},
    )

    assert docs_response.status_code == 401
    assert openapi_response.status_code == 401
    assert redoc_response.status_code == 401
    assert authorized_docs_response.status_code == 200
    assert authorized_openapi_response.status_code == 200
    assert authorized_openapi_response.json()["info"]["title"] == "File2Doc"
