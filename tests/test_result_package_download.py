from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_completed_job_package_download_contains_manifest_and_content(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("hello.txt", b"package me", "text/plain")},
    ).json()
    job = client.get(created["poll_url"]).json()

    response = client.get(job["result"]["package_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as package:
        assert sorted(package.namelist()) == [
            "artifacts.json",
            "content.md",
            "manifest.json",
        ]
        assert package.read("content.md") == b"package me"
