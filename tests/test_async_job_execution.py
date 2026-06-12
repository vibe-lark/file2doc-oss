import time

from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_upload_returns_before_slow_parser_finishes(tmp_path, monkeypatch):
    def slow_parse(source_path, content_type):
        time.sleep(0.4)
        return original_parse(source_path, content_type)

    import file2doc.store as store_module

    original_parse = store_module.parse_content_markdown
    monkeypatch.setattr(store_module, "parse_content_markdown", slow_parse)

    with TestClient(create_app(storage_root=tmp_path, auth_enabled=False)) as client:
        started_at = time.monotonic()
        create_response = client.post(
            "/parse-jobs/upload",
            files={"file": ("hello.txt", b"slow parser", "text/plain")},
        )
        elapsed = time.monotonic() - started_at

        assert create_response.status_code == 201
        assert elapsed < 0.3

        created = create_response.json()
        assert created["status"] == "queued"

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = client.get(created["poll_url"]).json()
            if job["status"] == "completed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"job did not complete: {job}")

        assert job["result"]["content_url"].startswith(
            f"/parse-jobs/{created['job_id']}/artifacts/"
        )
