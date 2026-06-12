from pathlib import Path

from fastapi.testclient import TestClient

from file2doc.app import create_app
from file2doc.audio import AudioParseOptions


def test_uploaded_audio_without_local_asr_configuration_fails_visibly(tmp_path, monkeypatch):
    monkeypatch.delenv("FILE2DOC_LOCAL_ASR_MODEL_DIR", raising=False)
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("meeting.wav", b"not a real wav", "audio/wav")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["error"]["code"] == "local_asr_not_configured"
    assert job["result"] is None


def test_uploaded_audio_with_local_asr_runner_produces_transcript_artifacts(tmp_path):
    def fake_runner(source_path: Path, options: AudioParseOptions):
        assert source_path.name == "meeting.wav"
        return {
            "text": "audio transcript marker",
            "segments": [{"start_sec": 0, "end_sec": 2.5, "text": "audio transcript marker"}],
            "engine": "fake-local-asr",
            "time_alignment_method": "fixture_segments",
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
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["transcript"]["engine"] == "fake-local-asr"
    assert manifest["transcript"]["time_aligned"] is True
    assert manifest["transcript"]["text_path"] == "transcripts/transcript.txt"
    assert manifest["transcript"]["segments_path"] == "transcripts/segments.json"
    assert manifest["media_index"] == []
    assert {
        artifact["kind"]
        for artifact in manifest["artifacts"]
    } >= {"content_markdown", "transcript_text", "transcript_segments"}

    content = client.get(job["result"]["content_url"]).text
    assert "audio transcript marker" in content


def test_uploaded_video_produces_transcript_frames_and_timeline(tmp_path):
    def fake_runner(source_path: Path, options: AudioParseOptions):
        assert source_path.name == "demo.mp4"
        return {
            "text": "video transcript marker",
            "segments": [{"start_sec": 1, "end_sec": 3, "text": "video transcript marker"}],
            "engine": "fake-video-asr",
            "time_alignment_method": "fixture_segments",
        }

    def fake_video_extractor(source_path: Path, output_dir: Path, **kwargs):
        artifact_id = kwargs["new_artifact_id"]()
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "frame_001.jpg").write_bytes(b"frame")
        return {
            "media_index": [
                {
                    "id": "video-frame-001",
                    "kind": "video_frame",
                    "path": "frame_001.jpg",
                    "artifact_id": artifact_id,
                    "media_type": "image/jpeg",
                    "source_ref": {"type": "video_time", "time_seconds": 1.0},
                    "derived": False,
                    "time_seconds": 1.0,
                    "width": 640,
                    "height": 360,
                }
            ],
            "timeline": [
                {
                    "id": "video-frame-001",
                    "kind": "video_frame",
                    "time_seconds": 1.0,
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
                    "time_seconds": 1.0,
                    "derived": False,
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
            video_frame_extractor=fake_video_extractor,
        )
    )

    created = client.post(
        "/parse-jobs/upload",
        files={"file": ("demo.mp4", b"not a real mp4", "video/mp4")},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    manifest = client.get(job["result"]["manifest_url"]).json()
    assert manifest["transcript"]["engine"] == "fake-video-asr"
    assert manifest["media_index"][0]["path"] == "images/video_frames/frame_001.jpg"
    assert manifest["timeline"][0]["path"] == "images/video_frames/frame_001.jpg"

    frame_artifact = next(
        artifact for artifact in manifest["artifacts"] if artifact["kind"] == "video_frame"
    )
    frame = client.get(f"/parse-jobs/{created['job_id']}/artifacts/{frame_artifact['artifact_id']}")
    assert frame.status_code == 200
    assert frame.content == b"frame"
