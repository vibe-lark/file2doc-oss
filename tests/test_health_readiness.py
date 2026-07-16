from fastapi.testclient import TestClient

from file2doc.audio import AudioParseOptions
from file2doc.app import create_app


def test_capabilities_is_public_and_reports_current_service_capabilities(
    tmp_path, monkeypatch
):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_text("model", encoding="utf-8")
    (model_dir / "tokens.txt").write_text("tokens", encoding="utf-8")
    monkeypatch.setenv("FILE2DOC_VISUAL_API_KEY", "visual-secret")
    monkeypatch.setenv("FILE2DOC_VISUAL_MODEL", "ep-visual")
    monkeypatch.setenv("FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS", "61")
    monkeypatch.setenv("FILE2DOC_VISUAL_MAX_CONCURRENCY", "3")
    client = TestClient(
        create_app(
            storage_root=tmp_path / "storage",
            auth_enabled=True,
            bearer_token="secret",
            audio_parse_options=AudioParseOptions(model_dir=model_dir),
        )
    )

    response = client.get("/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "supported_source_groups": ["pdf", "office", "text", "audio", "video", "image"],
        "auth_required": True,
        "storage_root": str(tmp_path / "storage"),
        "local_asr_configured": True,
        "local_asr_model_present": True,
        "ffmpeg_available": True,
        "visual_parsing_configured": True,
        "visual_model": "ep-visual",
        "visual_item_timeout_seconds": 17,
        "visual_job_deadline_seconds": 61,
        "visual_max_concurrency": 3,
        "page_image_dpi_options": [144, 216, 288],
        "page_image_dpi_default": 144,
    }


def test_healthz_is_public_and_returns_service_status(tmp_path):
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "file2doc"}


def test_readyz_is_public_and_checks_storage_and_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("FILE2DOC_LOCAL_ASR_MODEL_DIR", raising=False)
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "local_asr_model_present": False,
        "ffmpeg_available": True,
        "checks": {
            "storage_root": {"status": "ok"},
            "sqlite": {"status": "ok"},
        },
    }


def test_readyz_reports_missing_asr_model_without_failing_readiness(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "FILE2DOC_LOCAL_ASR_MODEL_DIR",
        str(tmp_path / "missing-asr-model"),
    )
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["local_asr_model_present"] is False
    assert isinstance(response.json()["ffmpeg_available"], bool)


def test_readyz_returns_unavailable_when_sqlite_cannot_round_trip(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    database_path = tmp_path / "file2doc.sqlite3"
    database_path.unlink()
    database_path.mkdir()

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"]["storage_root"] == {"status": "ok"}
    assert response.json()["checks"]["sqlite"]["status"] == "error"
    assert response.json()["checks"]["sqlite"]["detail"]


def test_readyz_returns_unavailable_when_storage_root_is_not_writable(tmp_path):
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))
    tmp_path.chmod(0o500)
    try:
        response = client.get("/readyz")
    finally:
        tmp_path.chmod(0o700)

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"]["storage_root"]["status"] == "error"
    assert response.json()["checks"]["storage_root"]["detail"]
