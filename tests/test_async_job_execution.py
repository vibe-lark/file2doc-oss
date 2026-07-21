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


def test_running_parser_updates_job_status_before_completion(tmp_path, monkeypatch):
    def slow_parse(source_path, content_type):
        time.sleep(0.5)
        return original_parse(source_path, content_type)

    import file2doc.store as store_module

    original_parse = store_module.parse_content_markdown
    monkeypatch.setattr(store_module, "parse_content_markdown", slow_parse)

    with TestClient(create_app(storage_root=tmp_path, auth_enabled=False)) as client:
        created = client.post(
            "/parse-jobs/upload",
            files={"file": ("hello.txt", b"slow parser", "text/plain")},
        ).json()

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            job = client.get(created["poll_url"]).json()
            if job["status"] == "running":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"job did not enter running state: {job}")

        assert job["stage"] == "parser_started"
        assert job["percent"] == 30
        assert job["latest_progress"]["stage"] == "parser_started"


def test_parse_job_metrics_report_queue_and_running_counts(tmp_path, monkeypatch):
    def slow_parse(source_path, content_type):
        time.sleep(0.5)
        return original_parse(source_path, content_type)

    import file2doc.store as store_module

    original_parse = store_module.parse_content_markdown
    monkeypatch.setattr(store_module, "parse_content_markdown", slow_parse)
    monkeypatch.setenv("FILE2DOC_MAX_CONCURRENT_JOBS", "1")

    with TestClient(create_app(storage_root=tmp_path, auth_enabled=False)) as client:
        first = client.post(
            "/parse-jobs/upload",
            files={"file": ("first.txt", b"first", "text/plain")},
        ).json()
        second = client.post(
            "/parse-jobs/upload",
            files={"file": ("second.txt", b"second", "text/plain")},
        ).json()

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            metrics = client.get("/metrics").json()
            if metrics["jobs"]["running"] == 1 and metrics["jobs"]["queued"] == 1:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"metrics did not expose queue state: {metrics}")

        assert metrics["jobs"]["max_concurrent"] == 1
        assert metrics["jobs"]["active_background_tasks"] >= 2

        running = client.get(first["poll_url"]).json()
        queued = client.get(second["poll_url"]).json()

        assert running["status"] == "running"
        assert running["queue"]["state"] == "running"
        assert queued["status"] == "queued"
        assert queued["queue"]["state"] == "queued"
        assert queued["queue"]["position"] == 1
