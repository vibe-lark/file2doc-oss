from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
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
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response

from file2doc.audio import AudioParseOptions
from file2doc.parsers import ParseOptions
from file2doc.rendering import AGENT_PAGE_IMAGE_DPI, ALLOWED_PAGE_IMAGE_DPI
from file2doc.store import JobStore
from file2doc.version import (
    SERVICE_VERSION,
    skill_version_payload,
    with_version_metadata,
)


def create_app(
    *,
    storage_root: str | Path = "/data/file2doc",
    auth_enabled: bool = True,
    bearer_token: str | None = None,
    audio_parse_options: AudioParseOptions | None = None,
    parse_options: ParseOptions | None = None,
    video_frame_extractor=None,
    store=None,
    execute_jobs_in_process: bool = True,
) -> FastAPI:
    root = Path(storage_root)
    capability_parse_options = parse_options or ParseOptions.from_env()
    if store is None:
        store = JobStore(
            root,
            audio_parse_options=audio_parse_options,
            parse_options=capability_parse_options,
            video_frame_extractor=video_frame_extractor,
        )
    app = FastAPI(
        title="File2Doc",
        version=SERVICE_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.file2doc_background_tasks = set()
    max_concurrent_jobs = _configured_max_concurrent_jobs()
    job_semaphore = asyncio.Semaphore(max_concurrent_jobs)

    async def require_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not auth_enabled:
            return
        expected = f"Bearer {bearer_token}" if bearer_token else None
        if expected is None or authorization != expected:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def openapi_schema() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        return app.openapi_schema

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def swagger_ui():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_auth)])
    async def redoc():
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - ReDoc",
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "service": "file2doc",
            "service_version": SERVICE_VERSION,
        }

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks = _readiness_checks(store, root)
        asr_engine = _configured_asr_engine(audio_parse_options)
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
                "service_version": SERVICE_VERSION,
                "local_asr_engine": asr_engine,
                "local_asr_model_present": _local_asr_model_present(
                    asr_model_dir,
                    asr_engine,
                ),
                "ffmpeg_available": _ffmpeg_available(),
                "visual_parsing_configured": (
                    capability_parse_options.visual_configured
                ),
                "checks": checks,
            },
        )

    @app.get("/capabilities")
    async def capabilities() -> dict:
        asr_engine = _configured_asr_engine(audio_parse_options)
        asr_model_dir = _configured_asr_model_dir(audio_parse_options)
        response = {
            "service_version": SERVICE_VERSION,
            "supported_source_groups": getattr(
                store,
                "supported_source_groups",
                ["pdf", "office", "text", "audio", "video"],
            ),
            "auth_required": auth_enabled,
            "storage_root": getattr(store, "storage_description", str(root)),
            "local_asr_engine": asr_engine,
            "local_asr_configured": asr_model_dir is not None,
            "local_asr_model_present": _local_asr_model_present(asr_model_dir, asr_engine),
            "transcript_segments_supported": True,
            "transcript_timestamps_supported": asr_engine == "funasr-local",
            "ffmpeg_available": _ffmpeg_available(),
            "visual_parsing_configured": capability_parse_options.visual_configured,
            "visual_provider": "ark-responses",
            "visual_model": capability_parse_options.visual_model,
            "visual_result_schema_version": "file2doc.visual-result.v1",
            "visual_tool_actions": {
                "zoom": True,
                "rotate": True,
                "point": False,
                "grounding": False,
            },
            "visual_item_timeout_seconds": (
                capability_parse_options.visual_item_timeout_seconds
            ),
            "visual_job_deadline_seconds": (
                capability_parse_options.visual_job_deadline_seconds
            ),
            "visual_max_concurrency": (
                capability_parse_options.visual_max_concurrency
            ),
            "page_image_dpi_options": sorted(ALLOWED_PAGE_IMAGE_DPI),
            "page_image_dpi_default": AGENT_PAGE_IMAGE_DPI,
        }
        max_upload_size_mb = _configured_max_upload_size_mb()
        if max_upload_size_mb is not None:
            response["max_upload_size_mb"] = max_upload_size_mb
        if hasattr(store, "runtime_name"):
            response["runtime"] = store.runtime_name
        return response

    @app.get("/skills/file2doc-http/version.json")
    async def file2doc_skill_version(installed_version: str | None = None) -> dict:
        return skill_version_payload(installed_version)

    @app.get("/metrics")
    async def metrics() -> dict:
        jobs = store.read_all_jobs()
        counts = _job_status_counts(jobs)
        response = {
            "jobs": {
                "queued": counts["queued"],
                "running": counts["running"],
                "completed": counts["completed"],
                "completed_with_warnings": counts["completed_with_warnings"],
                "failed": counts["failed"],
                "expired": counts["expired"],
                "total": len(jobs),
                "max_concurrent": max_concurrent_jobs,
                "active_background_tasks": len(app.state.file2doc_background_tasks),
                "runtime": getattr(store, "runtime_name", "local"),
            }
        }
        if hasattr(store, "runtime_metrics"):
            response["work_items"] = store.runtime_metrics()
        return response

    @app.post("/parse-jobs/upload", status_code=201, dependencies=[Depends(require_auth)])
    async def upload_parse_job(
        file: Annotated[UploadFile, File()],
        parser_profile: Annotated[str, Form()] = "agent",
        retention: Annotated[str, Form()] = "standard",
        skill_version: Annotated[
            str | None,
            Header(alias="X-File2Doc-Skill-Version"),
        ] = None,
    ) -> dict:
        if hasattr(store, "create_job_from_file"):
            job = await asyncio.to_thread(
                store.create_job_from_file,
                filename=file.filename or "source",
                content_type=file.content_type,
                source_file=file.file,
                parser_profile=parser_profile,
                retention=retention,
            )
        else:
            job = store.create_job(
                filename=file.filename or "source",
                content_type=file.content_type,
                source_bytes=await file.read(),
                parser_profile=parser_profile,
                retention=retention,
            )
        if execute_jobs_in_process:
            _schedule_job_completion(job["job_id"])
        return with_version_metadata(job, skill_version)

    def _schedule_job_completion(job_id: str) -> None:
        task = asyncio.create_task(_run_job_completion(job_id))
        app.state.file2doc_background_tasks.add(task)
        task.add_done_callback(app.state.file2doc_background_tasks.discard)

    async def _run_job_completion(job_id: str) -> None:
        async with job_semaphore:
            try:
                await asyncio.to_thread(store.complete_job, job_id)
            except Exception as error:
                store.fail_job(job_id, "job_execution_failed", str(error))

    @app.get("/parse-jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def get_parse_job(
        job_id: str,
        skill_version: Annotated[
            str | None,
            Header(alias="X-File2Doc-Skill-Version"),
        ] = None,
    ) -> dict:
        job = _with_queue_metadata(store.read_job(job_id), store.read_all_jobs())
        return with_version_metadata(job, skill_version)

    @app.get("/parse-jobs/{job_id}/events", dependencies=[Depends(require_auth)])
    async def get_parse_job_events(job_id: str, after: str | None = None) -> dict:
        return store.read_events(job_id, after=after)

    @app.get("/parse-jobs/{job_id}/result", dependencies=[Depends(require_auth)])
    async def get_parse_result(
        job_id: str,
        skill_version: Annotated[
            str | None,
            Header(alias="X-File2Doc-Skill-Version"),
        ] = None,
    ) -> dict:
        job = store.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "result_expired"})
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise HTTPException(status_code=409, detail={"code": "result_not_ready"})
        return with_version_metadata(store.read_manifest(job_id), skill_version)

    @app.get("/parse-jobs/{job_id}/artifacts/{artifact_id}", dependencies=[Depends(require_auth)])
    async def get_artifact(job_id: str, artifact_id: str) -> Response:
        artifact = store.read_artifact(job_id, artifact_id)
        if "download" in artifact:
            download = artifact["download"]
            return Response(
                download.content,
                media_type=download.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{download.filename}"'
                },
            )
        return FileResponse(
            artifact["absolute_path"],
            media_type=artifact["media_type"],
            filename=Path(artifact["path"]).name,
        )

    @app.get("/parse-jobs/{job_id}/package", dependencies=[Depends(require_auth)])
    async def get_package(job_id: str) -> Response:
        package = store.build_package_zip(job_id)
        if not isinstance(package, Path):
            return Response(
                package.content,
                media_type=package.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{package.filename}"'
                },
            )
        return FileResponse(
            package,
            media_type="application/zip",
            filename=f"{job_id}.zip",
        )

    @app.post("/admin/cleanup-expired", dependencies=[Depends(require_auth)])
    async def cleanup_expired_jobs() -> dict:
        return store.cleanup_expired_jobs()

    @app.post("/parse-jobs/{job_id}/assets/page-image", dependencies=[Depends(require_auth)])
    async def regenerate_page_image(
        job_id: str,
        request: Annotated[Any, Body()] = None,
    ) -> dict:
        if not isinstance(request, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_page_image_request"})
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


def _readiness_checks(store, root: Path) -> dict[str, dict[str, str]]:
    if hasattr(store, "check_readiness"):
        try:
            store.check_readiness()
        except Exception as error:
            return {"durable_runtime": {"status": "error", "detail": str(error)}}
        return {"durable_runtime": {"status": "ok"}}
    return {
        "storage_root": _check_storage_root(root),
        "sqlite": _check_sqlite(store),
    }


def _configured_asr_model_dir(options: AudioParseOptions | None) -> Path | None:
    if options is not None and options.model_dir is not None:
        return options.model_dir
    configured = os.environ.get("FILE2DOC_LOCAL_ASR_MODEL_DIR")
    if not configured:
        return None
    return Path(configured)


def _configured_asr_engine(options: AudioParseOptions | None) -> str:
    configured = None
    if options is not None:
        configured = options.engine
    configured = configured or os.environ.get("FILE2DOC_LOCAL_ASR_ENGINE")
    if not configured:
        return "funasr-local"
    engine = configured.strip().lower()
    if engine == "funasr":
        return "funasr-local"
    return engine


def _local_asr_model_present(model_dir: Path | None, engine: str = "funasr-local") -> bool:
    if model_dir is None:
        return False
    if engine == "funasr-local":
        return model_dir.exists()
    return False


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


def _job_status_counts(jobs: list[dict]) -> dict[str, int]:
    counts = {
        "queued": 0,
        "running": 0,
        "completed": 0,
        "completed_with_warnings": 0,
        "failed": 0,
        "expired": 0,
    }
    for job in jobs:
        status = job.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _with_queue_metadata(job: dict, jobs: list[dict]) -> dict:
    status = job.get("status")
    if status == "queued":
        queued_jobs = sorted(
            (
                candidate
                for candidate in jobs
                if candidate.get("status") == "queued"
            ),
            key=lambda candidate: (candidate.get("created_at", ""), candidate.get("job_id", "")),
        )
        position = next(
            (
                index
                for index, candidate in enumerate(queued_jobs, start=1)
                if candidate.get("job_id") == job.get("job_id")
            ),
            None,
        )
        return job | {"queue": {"state": "queued", "position": position}}
    if status == "running":
        return job | {"queue": {"state": "running", "position": None}}
    return job | {"queue": {"state": "terminal", "position": None}}
