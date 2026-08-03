from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
import shutil
from typing import Annotated, Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import PlainTextResponse

from file2doc.audio import AudioParseOptions
from file2doc.observability import CapacityMetrics
from file2doc.parsers import ParseOptions
from file2doc.rendering import AGENT_PAGE_IMAGE_DPI, ALLOWED_PAGE_IMAGE_DPI
from file2doc.store import JobStore


retention_logger = logging.getLogger("file2doc.retention")


def create_app(
    *,
    storage_root: str | Path = "/data/file2doc",
    auth_enabled: bool = True,
    bearer_token: str | None = None,
    audio_parse_options: AudioParseOptions | None = None,
    parse_options: ParseOptions | None = None,
    video_frame_extractor=None,
    diagnostic_cleanup_interval_seconds: float | None = None,
) -> FastAPI:
    root = Path(storage_root)
    capability_parse_options = parse_options or ParseOptions.from_env()
    max_concurrent_jobs = _configured_max_concurrent_jobs()
    metrics = CapacityMetrics(
        job_configured_concurrency=max_concurrent_jobs,
        visual_configured_concurrency=capability_parse_options.visual_max_concurrency,
    )
    capability_parse_options = capability_parse_options.with_metrics(metrics)
    store = JobStore(
        root,
        audio_parse_options=audio_parse_options,
        parse_options=capability_parse_options,
        video_frame_extractor=video_frame_extractor,
        visual_artifact_ttl_seconds=(
            capability_parse_options.visual_artifact_ttl_seconds
        ),
        visual_artifact_release_grace_seconds=(
            capability_parse_options.visual_artifact_release_grace_seconds
        ),
    )
    cleanup_interval_seconds = _configured_diagnostic_cleanup_interval_seconds(
        diagnostic_cleanup_interval_seconds
    )

    async def _cleanup_expired_loop() -> None:
        while True:
            await asyncio.sleep(cleanup_interval_seconds)
            try:
                await asyncio.to_thread(store.cleanup_expired_visual_diagnostics)
            except Exception:
                retention_logger.exception("Automatic retention cleanup failed")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        cleanup_task = asyncio.create_task(_cleanup_expired_loop())
        application.state.file2doc_diagnostic_cleanup_task = cleanup_task
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    app = FastAPI(title="File2Doc", version="0.1.0", lifespan=lifespan)
    app.state.file2doc_background_tasks = set()
    app.state.file2doc_metrics = metrics
    job_semaphore = asyncio.Semaphore(max_concurrent_jobs)

    async def require_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not auth_enabled:
            return
        expected = f"Bearer {bearer_token}" if bearer_token else None
        if expected is None or authorization != expected:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "file2doc"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics_endpoint() -> str:
        return metrics.render()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks = {
            "storage_root": _check_storage_root(root),
            "sqlite": _check_sqlite(store),
        }
        asr_model_dir = _configured_asr_model_dir(audio_parse_options)
        status = (
            "ok"
            if all(check["status"] == "ok" for check in checks.values())
            else "unavailable"
        )
        return JSONResponse(
            status_code=200 if status == "ok" else 503,
            content={
                "status": status,
                "local_asr_model_present": _local_asr_model_present(asr_model_dir),
                "ffmpeg_available": _ffmpeg_available(),
                "checks": checks,
            },
        )

    @app.get("/capabilities")
    async def capabilities() -> dict:
        asr_model_dir = _configured_asr_model_dir(audio_parse_options)
        response = {
            "supported_source_groups": [
                "pdf",
                "office",
                "text",
                "audio",
                "video",
                "image",
            ],
            "auth_required": auth_enabled,
            "job_max_concurrency": max_concurrent_jobs,
            "storage_root": str(root),
            "local_asr_configured": asr_model_dir is not None,
            "local_asr_model_present": _local_asr_model_present(asr_model_dir),
            "ffmpeg_available": _ffmpeg_available(),
            "visual_parsing_configured": capability_parse_options.visual_configured,
            "provider_roles": (
                {
                    "visual_understanding": {
                        "endpoint_role": "visual",
                        "provider": "ark-responses",
                        "model": capability_parse_options.visual_model,
                    }
                }
                if capability_parse_options.visual_configured
                else {}
            ),
            "visual_model": capability_parse_options.visual_model,
            "visual_item_timeout_seconds": (
                capability_parse_options.visual_item_timeout_seconds
            ),
            "visual_job_deadline_seconds": (
                capability_parse_options.visual_job_deadline_seconds
            ),
            "visual_max_concurrency": capability_parse_options.visual_max_concurrency,
            "visual_concurrency_scope": "process",
            "visual_artifact_ttl_seconds": (
                capability_parse_options.visual_artifact_ttl_seconds
            ),
            "visual_artifact_release_grace_seconds": (
                capability_parse_options.visual_artifact_release_grace_seconds
            ),
            "diagnostic_cleanup_interval_seconds": cleanup_interval_seconds,
            "visual_artifact_allowed_hosts": list(
                capability_parse_options.visual_artifact_allowed_hosts
            ),
            "page_image_dpi_options": sorted(ALLOWED_PAGE_IMAGE_DPI),
            "page_image_dpi_default": AGENT_PAGE_IMAGE_DPI,
        }
        max_upload_size_mb = _configured_max_upload_size_mb()
        if max_upload_size_mb is not None:
            response["max_upload_size_mb"] = max_upload_size_mb
        return response

    @app.post(
        "/parse-jobs/upload", status_code=201, dependencies=[Depends(require_auth)]
    )
    async def upload_parse_job(
        file: Annotated[UploadFile, File()],
        parser_profile: Annotated[str, Form()] = "agent",
        retention: Annotated[str, Form()] = "standard",
    ) -> dict:
        job = store.create_job(
            filename=file.filename or "source",
            content_type=file.content_type,
            source_bytes=await file.read(),
            parser_profile=parser_profile,
            retention=retention,
        )
        _schedule_job_completion(job["job_id"])
        return job

    def _schedule_job_completion(job_id: str) -> None:
        queued_at = metrics.job_queued()
        task = asyncio.create_task(_run_job_completion(job_id, queued_at))
        app.state.file2doc_background_tasks.add(task)
        task.add_done_callback(app.state.file2doc_background_tasks.discard)

    async def _run_job_completion(job_id: str, queued_at: float) -> None:
        async with job_semaphore:
            processing_started_at = metrics.job_started(queued_at=queued_at)
            store.mark_job_processing(job_id)
            try:
                await asyncio.to_thread(store.complete_job, job_id)
            except Exception as error:
                store.fail_job(job_id, "job_execution_failed", str(error))
            finally:
                status = store.read_job(job_id)["status"]
                metrics.job_finished(
                    succeeded=status in {"completed", "completed_with_warnings"},
                    duration_seconds=asyncio.get_running_loop().time() - queued_at,
                    processing_duration_seconds=(
                        asyncio.get_running_loop().time() - processing_started_at
                    ),
                )

    @app.get("/parse-jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def get_parse_job(job_id: str) -> dict:
        return store.read_job(job_id)

    @app.get("/parse-jobs/{job_id}/events", dependencies=[Depends(require_auth)])
    async def get_parse_job_events(job_id: str, after: str | None = None) -> dict:
        return store.read_events(job_id, after=after)

    @app.get("/parse-jobs/{job_id}/result", dependencies=[Depends(require_auth)])
    async def get_parse_result(job_id: str) -> dict:
        job = store.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "result_expired"})
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise HTTPException(status_code=409, detail={"code": "result_not_ready"})
        return store.read_manifest(job_id)

    @app.get(
        "/parse-jobs/{job_id}/artifacts/{artifact_id}",
        dependencies=[Depends(require_auth)],
    )
    async def get_artifact(job_id: str, artifact_id: str) -> FileResponse:
        artifact = store.read_artifact(job_id, artifact_id)
        return FileResponse(
            artifact["absolute_path"],
            media_type=artifact["media_type"],
            filename=Path(artifact["path"]).name,
        )

    @app.post(
        "/parse-jobs/{job_id}/diagnostics/release",
        dependencies=[Depends(require_auth)],
    )
    async def release_visual_diagnostics(job_id: str) -> dict:
        return store.release_visual_diagnostics(job_id)

    @app.get("/parse-jobs/{job_id}/package", dependencies=[Depends(require_auth)])
    async def get_package(job_id: str) -> FileResponse:
        package_path = store.build_package_zip(job_id)
        return FileResponse(
            package_path,
            media_type="application/zip",
            filename=f"{job_id}.zip",
        )

    @app.post("/admin/cleanup-expired", dependencies=[Depends(require_auth)])
    async def cleanup_expired_jobs() -> dict:
        return store.cleanup_expired_jobs()

    @app.post(
        "/parse-jobs/{job_id}/assets/page-image", dependencies=[Depends(require_auth)]
    )
    async def regenerate_page_image(
        job_id: str,
        request: Annotated[Any, Body()] = None,
    ) -> dict:
        if not isinstance(request, dict):
            raise HTTPException(
                status_code=400, detail={"code": "invalid_page_image_request"}
            )
        page = request.get("page")
        dpi = request.get("dpi")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise HTTPException(status_code=400, detail={"code": "invalid_page"})
        if (
            not isinstance(dpi, int)
            or isinstance(dpi, bool)
            or dpi not in ALLOWED_PAGE_IMAGE_DPI
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "unsupported_page_image_dpi"},
            )
        return store.regenerate_page_image(job_id, page=page, dpi=dpi)

    return app


def _check_storage_root(root: Path) -> dict[str, str]:
    probe_path = root / ".file2doc-readiness"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("ok", encoding="utf-8")
        if probe_path.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("storage readiness probe could not be read")
        probe_path.unlink()
    except Exception as error:
        return {"status": "error", "detail": str(error)}
    return {"status": "ok"}


def _check_sqlite(store: JobStore) -> dict[str, str]:
    try:
        store.check_sqlite_readiness()
    except Exception as error:
        return {"status": "error", "detail": str(error)}
    return {"status": "ok"}


def _configured_asr_model_dir(options: AudioParseOptions | None) -> Path | None:
    if options is not None and options.model_dir is not None:
        return options.model_dir
    configured = os.environ.get("FILE2DOC_LOCAL_ASR_MODEL_DIR")
    if not configured:
        return None
    return Path(configured)


def _configured_diagnostic_cleanup_interval_seconds(
    configured: float | None,
) -> float:
    value = (
        configured
        if configured is not None
        else float(os.getenv("FILE2DOC_DIAGNOSTIC_CLEANUP_INTERVAL_SECONDS", "60"))
    )
    if value <= 0:
        raise ValueError("diagnostic cleanup interval must be greater than zero")
    return value


def _local_asr_model_present(model_dir: Path | None) -> bool:
    if model_dir is None:
        return False
    candidates = [
        model_dir,
        model_dir / "sherpa-onnx-paraformer-zh-2023-03-28",
    ]
    return any(
        (candidate / "model.int8.onnx").is_file()
        and (candidate / "tokens.txt").is_file()
        for candidate in candidates
    )


def _ffmpeg_available() -> bool:
    if shutil.which("ffmpeg"):
        return True
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    try:
        return bool(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return False


def _configured_max_upload_size_mb() -> int | None:
    configured = os.environ.get("FILE2DOC_MAX_UPLOAD_SIZE_MB")
    if configured is None:
        return None
    try:
        return int(configured)
    except ValueError:
        return None


def _configured_max_concurrent_jobs() -> int:
    configured = os.environ.get("FILE2DOC_MAX_CONCURRENT_JOBS")
    if configured is None:
        return 2
    try:
        return max(1, int(configured))
    except ValueError:
        return 2
