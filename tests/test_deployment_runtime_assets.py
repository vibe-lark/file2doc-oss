from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_uses_amd64_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert not dockerfile.startswith("# syntax=")
    assert "FROM --platform=linux/amd64 python:3.12-slim" in dockerfile
    assert "PIP_PROGRESS_BAR=off" in dockerfile
    assert "PIP_DISABLE_PIP_VERSION_CHECK=1" in dockerfile
    assert "OPENBLAS_NUM_THREADS=1" in dockerfile
    assert "OMP_NUM_THREADS=1" in dockerfile
    assert '"--loop", "asyncio"' in dockerfile
    assert '"--http", "h11"' in dockerfile
    assert "mirrors.aliyun.com/pypi/simple" in dockerfile
    assert "download.pytorch.org/whl/cpu" in dockerfile
    assert "apt-get install" not in dockerfile
    assert "FILE2DOC_LOCAL_ASR_MODEL_DIR=/data/file2doc/models/funasr" in dockerfile
    assert "FILE2DOC_LOCAL_ASR_ENGINE=funasr-local" in dockerfile


def test_api_routes_are_async_to_avoid_runtime_threadpool_dependency() -> None:
    app_source = (ROOT / "src" / "file2doc" / "app.py").read_text(encoding="utf-8")

    assert "async def require_auth" in app_source
    assert "async def healthz" in app_source
    assert "async def readyz" in app_source
    assert "async def get_parse_job" in app_source
    assert "async def get_artifact" in app_source
    assert "async def cleanup_expired_jobs" in app_source
