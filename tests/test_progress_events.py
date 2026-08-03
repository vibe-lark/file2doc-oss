from pathlib import Path

from fastapi.testclient import TestClient

from file2doc.app import create_app
from file2doc.audio import AudioParseOptions


def test_parse_job_exposes_progress_event_history(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("notes.txt", b"visible progress", "text/plain")},
    ).json()

    response = client.get(f"/parse-jobs/{created['job_id']}/events")

    assert response.status_code == 200
    body = response.json()
    assert body["next_after"] == body["events"][-1]["event_id"]
    assert [event["stage"] for event in body["events"]] == [
        "queued",
        "intaking",
        "processing",
        "parser_started",
        "assembling",
        "completed",
    ]
    assert body["events"][0]["percent"] == 0
    assert body["events"][-1]["percent"] == 100


def test_audio_parse_job_reports_asr_progress_before_assembling(tmp_path):
    def fake_runner(source_path: Path, options: AudioParseOptions):
        return {
            "text": "audio transcript marker",
            "segments": [{"start_sec": 0, "end_sec": 1, "text": "audio transcript marker"}],
            "engine": "fake-local-asr",
        }

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            audio_parse_options=AudioParseOptions(
                model_dir=tmp_path / "model",
                runner=fake_runner,
            ),
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("meeting.wav", b"not a real wav", "audio/wav")},
    ).json()

    response = client.get(f"/parse-jobs/{created['job_id']}/events")

    assert response.status_code == 200
    assert [event["stage"] for event in response.json()["events"]] == [
        "queued",
        "intaking",
        "processing",
        "asr_running",
        "assembling",
        "completed",
    ]


def test_video_parse_job_reports_asr_and_frame_progress_before_assembling(tmp_path):
    def fake_runner(source_path: Path, options: AudioParseOptions):
        return {
            "text": "video transcript marker",
            "segments": [{"start_sec": 0, "end_sec": 1, "text": "video transcript marker"}],
            "engine": "fake-local-asr",
        }

    def fake_frame_extractor(source_path: Path, output_dir: Path, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_path = output_dir / "frame_001.jpg"
        frame_path.write_bytes(b"fake jpeg")
        artifact_id = kwargs["new_artifact_id"]()
        return {
            "media_index": [
                {
                    "id": "video-frame-001",
                    "kind": "video_frame",
                    "path": "frame_001.jpg",
                    "artifact_id": artifact_id,
                    "media_type": "image/jpeg",
                    "time_seconds": 0.0,
                }
            ],
            "timeline": [
                {
                    "id": "video-frame-001",
                    "kind": "video_frame",
                    "time_seconds": 0.0,
                    "media_id": "video-frame-001",
                    "artifact_id": artifact_id,
                    "path": "frame_001.jpg",
                }
            ],
            "artifacts": {
                artifact_id: {
                    "artifact_id": artifact_id,
                    "kind": "video_frame",
                    "path": "frame_001.jpg",
                    "media_type": "image/jpeg",
                    "time_seconds": 0.0,
                }
            },
        }

    client = TestClient(
        create_app(
            storage_root=tmp_path,
            auth_enabled=False,
            audio_parse_options=AudioParseOptions(
                model_dir=tmp_path / "model",
                runner=fake_runner,
            ),
            video_frame_extractor=fake_frame_extractor,
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("meeting.mp4", b"not a real mp4", "video/mp4")},
    ).json()

    response = client.get(f"/parse-jobs/{created['job_id']}/events")

    assert response.status_code == 200
    assert [event["stage"] for event in response.json()["events"]] == [
        "queued",
        "intaking",
        "processing",
        "asr_running",
        "video_frame_extracting",
        "assembling",
        "completed",
    ]
