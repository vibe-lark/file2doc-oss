from fastapi.testclient import TestClient

from file2doc.app import create_app


def test_completed_job_state_result_and_events_survive_app_recreation(tmp_path):
    first_client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    created = first_client.post(
        "/parse-jobs/upload",
        files={"file": ("persist.txt", b"persistent content", "text/plain")},
    ).json()
    completed = first_client.get(created["poll_url"]).json()
    first_events = first_client.get(f"/parse-jobs/{created['job_id']}/events").json()
    job_root = tmp_path / "jobs" / created["job_id"]
    (job_root / "job.json").unlink()
    (job_root / "events.json").unlink()

    restarted_client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    job_response = restarted_client.get(created["poll_url"])
    result_response = restarted_client.get(completed["result"]["manifest_url"])
    artifact_response = restarted_client.get(completed["result"]["content_url"])
    events_response = restarted_client.get(f"/parse-jobs/{created['job_id']}/events")

    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "completed"
    assert job["result"] == completed["result"]

    assert result_response.status_code == 200
    assert (
        result_response.json()["content"]["artifact_id"]
        == completed["result"]["content_artifact_id"]
    )

    assert artifact_response.status_code == 200
    assert artifact_response.text == "persistent content"

    assert events_response.status_code == 200
    events = events_response.json()
    assert events == first_events
    assert [event["stage"] for event in events["events"]] == [
        "queued",
        "intaking",
        "parser_started",
        "assembling",
        "completed",
    ]
