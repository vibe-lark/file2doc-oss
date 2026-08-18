from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import sqlite3
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from file2doc.audio import AudioParseFailure, AudioParseOptions, parse_audio_transcript
from file2doc.office_rendering import (
    OfficeRenderError,
    is_pptx_source,
    render_pptx_visual_assets,
)
from file2doc.parsers import ParseFailure, ParseOptions, parse_content_markdown
from file2doc.rendering import (
    AGENT_PAGE_IMAGE_DPI,
    AGENT_THUMBNAIL_MAX_EDGE,
    PageRenderError,
    is_pdf_source,
    render_pdf_page_image,
    render_pdf_visual_assets,
)
from file2doc.video import VideoFrameExtractionError, VideoToolUnavailable, extract_video_frames
from file2doc.version import skill_version_payload
from file2doc_markitdown_visual.plugin import (
    VISUAL_PROMPT_VERSION,
    VISUAL_RESULT_SCHEMA_VERSION,
)

SCHEMA_VERSION = "file2doc.parse-result.v1"
VideoFrameExtractor = Callable[..., dict]
OfficePageRenderer = Callable[..., tuple[list[dict], list[dict], dict[str, dict]]]


class JobStore:
    def __init__(
        self,
        storage_root: Path,
        *,
        audio_parse_options: AudioParseOptions | None = None,
        parse_options: ParseOptions | None = None,
        video_frame_extractor: VideoFrameExtractor | None = None,
        office_page_renderer: OfficePageRenderer | None = None,
    ) -> None:
        self.storage_root = storage_root
        self.audio_parse_options = audio_parse_options
        self.parse_options = parse_options
        self.video_frame_extractor = video_frame_extractor or extract_video_frames
        self.office_page_renderer = office_page_renderer or render_pptx_visual_assets
        self.jobs_root = storage_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.database_path = storage_root / "file2doc.sqlite3"
        self._init_database()

    def create_job(
        self,
        *,
        filename: str,
        content_type: str | None,
        source_bytes: bytes,
        parser_profile: str,
        retention: str,
    ) -> dict:
        now = _now()
        job_id = _id("job")
        job_root = self._job_root(job_id)
        source_root = job_root / "source"
        source_root.mkdir(parents=True, exist_ok=True)
        source_path = source_root / filename
        source_path.write_bytes(source_bytes)

        content_identity = hashlib.sha256(source_bytes).hexdigest()
        expires_at = now + _retention_delta(retention)
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "poll_url": f"/parse-jobs/{job_id}",
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
            "source": {
                "filename": filename,
                "content_type": content_type
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream",
                "sha256": content_identity,
                "size_bytes": len(source_bytes),
                "path": f"source/{filename}",
            },
            "parser_profile": parser_profile,
            "retention": retention,
            "latest_progress": {
                "stage": "queued",
                "percent": 0,
                "message": "Parse job accepted",
                "detail": {},
                "created_at": _iso(now),
            },
            "warnings_count": 0,
            "error": None,
            "result": None,
        }
        self._persist_job(job)
        self._write_json(job_root / "job.json", job)
        self._append_event(job_id, "queued", 0, "Parse job accepted")
        self._append_event(job_id, "intaking", 10, "Source file stored")
        return self._public_create_response(job)

    def complete_job(self, job_id: str) -> None:
        job = self.read_job(job_id)
        job_root = self._job_root(job_id)
        result_root = job_root / "result-package"
        result_root.mkdir(parents=True, exist_ok=True)

        source_path = job_root / job["source"]["path"]
        content_type = job["source"]["content_type"]
        if _is_audio_source(source_path, content_type):
            self._complete_audio_job(job, source_path, result_root)
            return
        if _is_video_source(source_path, content_type):
            self._complete_video_job(job, source_path, result_root)
            return

        self.mark_job_running(job_id, "parser_started", 30, "Parser started")
        job = self.read_job(job_id)
        try:
            parsed = parse_content_markdown(
                source_path,
                content_type,
                self.parse_options,
            )
        except ParseFailure as error:
            self._fail_job(job, error.code, str(error))
            return

        content_artifact_id = _id("art")
        content_path = result_root / "content.md"
        content_path.write_text(parsed.markdown, encoding="utf-8")

        artifacts = {
            content_artifact_id: {
                "artifact_id": content_artifact_id,
                "kind": "content_markdown",
                "path": "content.md",
                "media_type": "text/markdown; charset=utf-8",
            }
        }
        page_index: list[dict] = []
        media_index: list[dict] = []
        source_is_pdf = is_pdf_source(source_path, job["source"]["content_type"])
        source_is_pptx = is_pptx_source(source_path, job["source"]["content_type"])
        if source_is_pdf:
            page_index, media_index, visual_artifacts = render_pdf_visual_assets(
                source_path,
                result_root,
                new_artifact_id=lambda: _id("art"),
                visual_results=parsed.visual_results,
                visual_configured=bool(
                    self.parse_options and self.parse_options.visual_configured
                ),
            )
            artifacts.update(visual_artifacts)
        elif source_is_pptx:
            try:
                page_index, media_index, visual_artifacts = (
                    self.office_page_renderer(
                        source_path,
                        result_root,
                        new_artifact_id=lambda: _id("art"),
                    )
                )
            except OfficeRenderError as error:
                self._fail_job(job, error.code, str(error))
                return
            artifacts.update(visual_artifacts)
            content_path.write_text(
                _bind_pptx_markdown_to_source_units(parsed.markdown, page_index),
                encoding="utf-8",
            )

        visual_media, visual_result_artifacts = self._write_visual_results(
            parsed,
            result_root=result_root,
            publish_source_media=not source_is_pdf,
            source_units=page_index if source_is_pptx else None,
        )
        media_index.extend(visual_media)
        artifacts.update(visual_result_artifacts)

        self.mark_job_running(job_id, "assembling", 80, "Assembling result package")
        job = self.read_job(job_id)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            **skill_version_payload(),
            "job_id": job_id,
            "source": job["source"],
            "options": {
                "page_image_dpi": AGENT_PAGE_IMAGE_DPI,
                "thumbnail_max_edge": AGENT_THUMBNAIL_MAX_EDGE,
            },
            "content": {
                "artifact_id": content_artifact_id,
                "path": "content.md",
                "media_type": "text/markdown; charset=utf-8",
            },
            "parser": parsed.diagnostics,
            "page_index": page_index,
            "media_index": media_index,
            "artifacts": list(artifacts.values()),
            "warnings": parsed.warnings,
            "created_at": _iso(_now()),
            "expires_at": job["expires_at"],
        }
        self._write_json(result_root / "manifest.json", manifest)
        self._write_json(result_root / "artifacts.json", artifacts)

        final_status = "completed_with_warnings" if parsed.warnings else "completed"
        job["status"] = final_status
        job["stage"] = final_status
        job["percent"] = 100
        job["warnings_count"] = len(parsed.warnings)
        job["latest_progress"] = {
            "stage": final_status,
            "percent": 100,
            "message": "Result package assembled",
            "detail": {},
            "created_at": _iso(_now()),
        }
        job["result"] = {
            "manifest_url": f"/parse-jobs/{job_id}/result",
            "package_url": f"/parse-jobs/{job_id}/package",
            "content_artifact_id": content_artifact_id,
            "content_url": f"/parse-jobs/{job_id}/artifacts/{content_artifact_id}",
        }
        self._persist_job(job)
        self._write_json(job_root / "job.json", job)
        self._append_event(job_id, final_status, 100, "Result package assembled")

    def _write_visual_results(
        self,
        parsed,
        *,
        result_root: Path,
        publish_source_media: bool,
        source_units: list[dict] | None = None,
    ) -> tuple[list[dict], dict[str, dict]]:
        if not publish_source_media:
            return [], {}
        media_index: list[dict] = []
        artifacts: dict[str, dict] = {}
        results_by_ref = {result.source_ref: result for result in parsed.visual_results}

        for source in parsed.visual_sources:
            occurrence_refs = [
                _document_position_source_ref(location, source_units)
                for location in (source.locations or ("unknown document position",))
            ]
            extension = ".jpg" if source.media_type in {"image/jpeg", "image/jpg"} else ".png"
            image_path = Path(f"images/visual/{source.content_sha256}{extension}")
            absolute_image_path = result_root / image_path
            absolute_image_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_image_path.write_bytes(source.content)
            image_artifact_id = _id("art")
            image_artifact = {
                "artifact_id": image_artifact_id,
                "kind": "visual_source_image",
                "path": image_path.as_posix(),
                "media_type": source.media_type,
                "source_units": occurrence_refs,
                "sha256": source.content_sha256,
            }
            artifacts[image_artifact_id] = image_artifact

            result = results_by_ref.get(source.source_ref)
            result_artifact_id = None
            result_path = None
            if result is not None:
                result_artifact_id = _id("art")
                result_path = Path(f"visual/{source.content_sha256}.json")
                payload = _visual_result_payload(result, source_units=occurrence_refs)
                self._write_json(result_root / result_path, payload)
                artifacts[result_artifact_id] = {
                    "artifact_id": result_artifact_id,
                    "kind": "visual_parse_result",
                    "path": result_path.as_posix(),
                    "media_type": "application/json; charset=utf-8",
                    "source_ref": source.source_ref,
                    "source_units": occurrence_refs,
                    "sha256": source.content_sha256,
                }

            for occurrence, (location, occurrence_ref) in enumerate(
                zip(
                    source.locations or ("unknown document position",),
                    occurrence_refs,
                ),
                1,
            ):
                base_media_id = f"visual-item-{source.content_sha256[:16]}"
                media_id = (
                    base_media_id
                    if occurrence == 1
                    else f"{base_media_id}-{occurrence:03d}"
                )
                media_index.append(
                    {
                        "id": media_id,
                        "kind": "embedded_image",
                        "path": image_path.as_posix(),
                        "artifact_id": image_artifact_id,
                        "media_type": source.media_type,
                        "source_ref": occurrence_ref,
                        "visual_parse_status": (
                            "completed" if result is not None else "warning"
                        ),
                        "visual_result_artifact_id": result_artifact_id,
                        "visual_result_path": (
                            result_path.as_posix() if result_path is not None else None
                        ),
                        "derived": False,
                    }
                )

        return media_index, artifacts

    def _complete_audio_job(self, job: dict, source_path: Path, result_root: Path) -> None:
        self.mark_job_running(job["job_id"], "asr_running", 30, "ASR transcription running")
        job = self.read_job(job["job_id"])
        try:
            transcript = parse_audio_transcript(
                source_path,
                job["source"]["content_type"],
                self.audio_parse_options,
            )
        except AudioParseFailure as error:
            self._fail_job(job, error.code, str(error))
            return

        manifest_transcript = transcript.to_manifest_transcript()
        content_markdown = _transcript_markdown(manifest_transcript)
        self.mark_job_running(job["job_id"], "assembling", 80, "Assembling result package")
        job = self.read_job(job["job_id"])
        self._write_media_result(
            job,
            result_root,
            content_markdown=content_markdown,
            parser={
                "name": "local-asr",
                "engine": transcript.engine,
                **transcript.diagnostics,
            },
            transcript=manifest_transcript,
            page_index=[],
            media_index=[],
            timeline=[],
            artifacts={},
            warnings=_transcript_warnings(manifest_transcript),
        )

    def _complete_video_job(self, job: dict, source_path: Path, result_root: Path) -> None:
        self.mark_job_running(job["job_id"], "asr_running", 30, "ASR transcription running")
        job = self.read_job(job["job_id"])
        try:
            transcript = parse_audio_transcript(
                source_path,
                job["source"]["content_type"],
                self.audio_parse_options,
            )
        except AudioParseFailure as error:
            self._fail_job(job, error.code, str(error))
            return

        frame_root = result_root / "images" / "video_frames"
        self.mark_job_running(
            job["job_id"],
            "video_frame_extracting",
            60,
            "Video frame extraction running",
        )
        job = self.read_job(job["job_id"])
        try:
            frame_result = self.video_frame_extractor(
                source_path,
                frame_root,
                new_artifact_id=lambda: _id("art"),
            )
        except (VideoToolUnavailable, VideoFrameExtractionError) as error:
            self._fail_job(job, "video_frame_extraction_failed", str(error))
            return

        frame_result = _prefix_video_frame_paths(frame_result, "images/video_frames")
        manifest_transcript = transcript.to_manifest_transcript()
        content_markdown = _video_markdown(manifest_transcript, frame_result["media_index"])
        self.mark_job_running(job["job_id"], "assembling", 80, "Assembling result package")
        job = self.read_job(job["job_id"])
        self._write_media_result(
            job,
            result_root,
            content_markdown=content_markdown,
            parser={
                "name": "local-asr+ffmpeg",
                "engine": transcript.engine,
                **transcript.diagnostics,
            },
            transcript=manifest_transcript,
            page_index=[],
            media_index=frame_result["media_index"],
            timeline=frame_result["timeline"],
            artifacts=frame_result["artifacts"],
            warnings=_transcript_warnings(manifest_transcript),
        )

    def _write_media_result(
        self,
        job: dict,
        result_root: Path,
        *,
        content_markdown: str,
        parser: dict,
        transcript: dict,
        page_index: list[dict],
        media_index: list[dict],
        timeline: list[dict],
        artifacts: dict[str, dict],
        warnings: list[dict],
    ) -> None:
        job_id = job["job_id"]
        content_artifact_id = _id("art")
        transcript_text_artifact_id = _id("art")
        transcript_segments_artifact_id = _id("art")

        content_path = result_root / "content.md"
        transcript_text_path = result_root / "transcripts" / "transcript.txt"
        transcript_segments_path = result_root / "transcripts" / "segments.json"
        content_path.write_text(content_markdown, encoding="utf-8")
        transcript_text_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_text_path.write_text(transcript["text"], encoding="utf-8")
        transcript_segments_path.write_text(
            json.dumps(transcript["segments"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        transcript = transcript | {
            "text_path": "transcripts/transcript.txt",
            "segments_path": "transcripts/segments.json",
        }
        all_artifacts = {
            content_artifact_id: {
                "artifact_id": content_artifact_id,
                "kind": "content_markdown",
                "path": "content.md",
                "media_type": "text/markdown; charset=utf-8",
            },
            transcript_text_artifact_id: {
                "artifact_id": transcript_text_artifact_id,
                "kind": "transcript_text",
                "path": "transcripts/transcript.txt",
                "media_type": "text/plain; charset=utf-8",
            },
            transcript_segments_artifact_id: {
                "artifact_id": transcript_segments_artifact_id,
                "kind": "transcript_segments",
                "path": "transcripts/segments.json",
                "media_type": "application/json; charset=utf-8",
            },
            **artifacts,
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            **skill_version_payload(),
            "job_id": job_id,
            "source": job["source"],
            "options": {
                "page_image_dpi": AGENT_PAGE_IMAGE_DPI,
                "thumbnail_max_edge": AGENT_THUMBNAIL_MAX_EDGE,
            },
            "content": {
                "artifact_id": content_artifact_id,
                "path": "content.md",
                "media_type": "text/markdown; charset=utf-8",
            },
            "parser": parser,
            "transcript": transcript,
            "page_index": page_index,
            "media_index": media_index,
            "timeline": timeline,
            "artifacts": list(all_artifacts.values()),
            "warnings": warnings,
            "created_at": _iso(_now()),
            "expires_at": job["expires_at"],
        }
        self._write_json(result_root / "manifest.json", manifest)
        self._write_json(result_root / "artifacts.json", all_artifacts)
        self._complete_job_record(job, content_artifact_id)

    def _complete_job_record(self, job: dict, content_artifact_id: str) -> None:
        job_id = job["job_id"]
        job["status"] = "completed"
        job["stage"] = "completed"
        job["percent"] = 100
        job["latest_progress"] = {
            "stage": "completed",
            "percent": 100,
            "message": "Result package assembled",
            "detail": {},
            "created_at": _iso(_now()),
        }
        job["result"] = {
            "manifest_url": f"/parse-jobs/{job_id}/result",
            "package_url": f"/parse-jobs/{job_id}/package",
            "content_artifact_id": content_artifact_id,
            "content_url": f"/parse-jobs/{job_id}/artifacts/{content_artifact_id}",
        }
        self._persist_job(job)
        self._write_json(self._job_root(job_id) / "job.json", job)
        self._append_event(job_id, "completed", 100, "Result package assembled")

    def _fail_job(self, job: dict, code: str, message: str) -> None:
        job["status"] = "failed"
        job["stage"] = "failed"
        job["percent"] = 100
        job["latest_progress"] = {
            "stage": "failed",
            "percent": 100,
            "message": message,
            "detail": {"code": code},
            "created_at": _iso(_now()),
        }
        job["error"] = {"code": code, "message": message}
        self._persist_job(job)
        self._write_json(self._job_root(job["job_id"]) / "job.json", job)
        self._append_event(job["job_id"], "failed", 100, message)

    def fail_job(self, job_id: str, code: str, message: str) -> None:
        self._fail_job(self.read_job(job_id), code, message)

    def mark_job_running(
        self,
        job_id: str,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        job = self.read_job(job_id)
        if job["status"] in {"completed", "completed_with_warnings", "failed", "expired"}:
            return
        job["status"] = "running"
        job["stage"] = stage
        job["percent"] = percent
        job["latest_progress"] = {
            "stage": stage,
            "percent": percent,
            "message": message,
            "detail": {},
            "created_at": _iso(_now()),
        }
        self._persist_job(job)
        self._write_json(self._job_root(job_id) / "job.json", job)
        self._append_event(job_id, stage, percent, message)

    def read_job(self, job_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "select document from jobs where job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found"})
        return json.loads(row["document"])

    def read_all_jobs(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("select document from jobs").fetchall()
        return [json.loads(row["document"]) for row in rows]

    def read_events(self, job_id: str, *, after: str | None = None) -> dict:
        self.read_job(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select document from events where job_id = ? order by sequence",
                (job_id,),
            ).fetchall()
        events = [json.loads(row["document"]) for row in rows]
        if after is not None:
            events = _events_after(events, after)
        return {
            "events": events,
            "next_after": events[-1]["event_id"] if events else after,
        }

    def read_manifest(self, job_id: str) -> dict:
        job = self.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "result_expired"})
        return self._read_json(self._job_root(job_id) / "result-package" / "manifest.json")

    def read_artifact(self, job_id: str, artifact_id: str) -> dict:
        job = self.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "artifact_expired"})
        artifacts = self._read_json(self._job_root(job_id) / "result-package" / "artifacts.json")
        try:
            artifact = artifacts[artifact_id]
        except KeyError as error:
            raise HTTPException(status_code=404, detail={"code": "artifact_not_found"}) from error
        if artifact.get("expires_at") and _parse_iso(artifact["expires_at"]) <= _now():
            absolute_path = self._job_root(job_id) / "result-package" / artifact["path"]
            absolute_path.unlink(missing_ok=True)
            raise HTTPException(status_code=410, detail={"code": "artifact_expired"})
        absolute_path = self._job_root(job_id) / "result-package" / artifact["path"]
        return artifact | {"absolute_path": absolute_path}

    def regenerate_page_image(self, job_id: str, *, page: int, dpi: int) -> dict:
        job = self.read_job(job_id)
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise HTTPException(status_code=409, detail={"code": "result_not_ready"})

        source_path = self._job_root(job_id) / job["source"]["path"]
        if not is_pdf_source(source_path, job["source"]["content_type"]):
            raise HTTPException(status_code=400, detail={"code": "source_not_pdf"})

        result_root = self._job_root(job_id) / "result-package"
        manifest_path = result_root / "manifest.json"
        artifacts_path = result_root / "artifacts.json"
        manifest = self._read_json(manifest_path)
        artifacts = self._read_json(artifacts_path)

        media_id = f"page-{page}-image-{dpi}dpi"
        existing_media = next(
            (item for item in manifest["media_index"] if item["id"] == media_id),
            None,
        )
        artifact_id = existing_media["artifact_id"] if existing_media else _id("art")
        try:
            media, artifact = render_pdf_page_image(
                source_path,
                result_root,
                page_number=page,
                dpi=dpi,
                artifact_id=artifact_id,
            )
        except PageRenderError as error:
            raise HTTPException(status_code=400, detail={"code": str(error)}) from error

        artifacts[artifact_id] = artifact
        if existing_media is None:
            manifest["media_index"].append(media)
        else:
            manifest["media_index"] = [
                media if item["id"] == media_id else item for item in manifest["media_index"]
            ]
        manifest["artifacts"] = list(artifacts.values())

        self._write_json(manifest_path, manifest)
        self._write_json(artifacts_path, artifacts)

        return {
            "artifact_id": artifact_id,
            "media_id": media["id"],
            "path": media["path"],
            "media_type": media["media_type"],
            "page": page,
            "dpi": dpi,
            "artifact_url": f"/parse-jobs/{job_id}/artifacts/{artifact_id}",
        }

    def check_sqlite_readiness(self) -> None:
        probe_id = _id("ready")
        with self._connect() as connection:
            connection.execute(
                """
                insert into readiness_probe (probe_id, created_at)
                values (?, ?)
                """,
                (probe_id, _iso(_now())),
            )
            row = connection.execute(
                "select probe_id from readiness_probe where probe_id = ?",
                (probe_id,),
            ).fetchone()
            if row is None or row["probe_id"] != probe_id:
                raise RuntimeError("readiness probe row could not be read")
            connection.execute(
                "delete from readiness_probe where probe_id = ?",
                (probe_id,),
            )

    def build_package_zip(self, job_id: str) -> Path:
        job = self.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "artifact_expired"})
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise HTTPException(status_code=409, detail={"code": "result_not_ready"})

        result_root = self._job_root(job_id) / "result-package"
        package_path = self._job_root(job_id) / f"{job_id}.zip"
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(result_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(result_root).as_posix())
        return package_path

    def cleanup_expired_jobs(self) -> dict:
        now = _now()
        scanned_count = 0
        expired_count = 0
        deleted_bytes = 0
        deleted_jobs: list[str] = []

        with self._connect() as connection:
            rows = connection.execute("select document from jobs").fetchall()

        for row in rows:
            job = json.loads(row["document"])
            scanned_count += 1
            if job["status"] == "expired" or _parse_iso(job["expires_at"]) > now:
                continue

            job_id = job["job_id"]
            job_root = self._job_root(job_id)
            deleted_bytes += _directory_size(job_root)
            if job_root.exists():
                shutil.rmtree(job_root)

            job["status"] = "expired"
            job["stage"] = "expired"
            job["percent"] = 100
            job["latest_progress"] = {
                "stage": "expired",
                "percent": 100,
                "message": "Job expired and service-side files were removed",
                "detail": {"code": "job_expired"},
                "created_at": _iso(now),
            }
            job["error"] = {
                "code": "job_expired",
                "message": "Job expired and service-side files were removed",
            }
            self._persist_job(job)
            self._append_event(
                job_id,
                "expired",
                100,
                "Job expired and service-side files were removed",
                write_legacy_file=False,
            )
            expired_count += 1
            deleted_jobs.append(job_id)

        return {
            "scanned_count": scanned_count,
            "expired_count": expired_count,
            "deleted_bytes": deleted_bytes,
            "deleted_jobs": deleted_jobs,
        }

    def _init_database(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute(
                """
                create table if not exists jobs (
                    job_id text primary key,
                    document text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists events (
                    event_id text primary key,
                    job_id text not null,
                    sequence integer not null,
                    document text not null,
                    foreign key (job_id) references jobs (job_id)
                )
                """
            )
            connection.execute(
                "create index if not exists events_job_sequence on events (job_id, sequence)"
            )
            connection.execute(
                """
                create table if not exists readiness_probe (
                    probe_id text primary key,
                    created_at text not null
                )
                """
            )
        self._import_legacy_metadata()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _persist_job(self, job: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into jobs (job_id, document)
                values (?, ?)
                on conflict(job_id) do update set document = excluded.document
                """,
                (job["job_id"], json.dumps(job, ensure_ascii=False)),
            )

    def _import_legacy_metadata(self) -> None:
        for job_path in self.jobs_root.glob("*/job.json"):
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue

            job_id = job.get("job_id")
            if not isinstance(job_id, str):
                continue

            events_path = job_path.parent / "events.json"
            events: list[dict] = []
            if events_path.exists():
                try:
                    events = json.loads(events_path.read_text(encoding="utf-8"))["events"]
                except (KeyError, json.JSONDecodeError):
                    events = []

            with self._connect() as connection:
                connection.execute(
                    """
                    insert or ignore into jobs (job_id, document)
                    values (?, ?)
                    """,
                    (job_id, json.dumps(job, ensure_ascii=False)),
                )
                for sequence, event in enumerate(events, start=1):
                    event_id = event.get("event_id") if isinstance(event, dict) else None
                    if not isinstance(event_id, str):
                        continue
                    connection.execute(
                        """
                        insert or ignore into events (event_id, job_id, sequence, document)
                        values (?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            job_id,
                            sequence,
                            json.dumps(event, ensure_ascii=False),
                        ),
                    )

    def _job_root(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def _read_json(self, path: Path) -> dict:
        if not path.exists():
            raise HTTPException(status_code=404, detail={"code": "job_not_found"})
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_event(
        self,
        job_id: str,
        stage: str,
        percent: int,
        message: str,
        *,
        write_legacy_file: bool = True,
    ) -> None:
        event = {
            "event_id": _id("evt"),
            "stage": stage,
            "percent": percent,
            "message": message,
            "detail": {},
            "created_at": _iso(_now()),
        }
        with self._connect() as connection:
            row = connection.execute(
                "select coalesce(max(sequence), 0) + 1 as next_sequence from events where job_id = ?",
                (job_id,),
            ).fetchone()
            connection.execute(
                """
                insert into events (event_id, job_id, sequence, document)
                values (?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    job_id,
                    row["next_sequence"],
                    json.dumps(event, ensure_ascii=False),
                ),
            )

        if not write_legacy_file:
            return

        events_path = self._job_root(job_id) / "events.json"
        if events_path.exists():
            events = self._read_json(events_path)["events"]
        else:
            events = []
        events.append(event)
        self._write_json(events_path, {"events": events})

    def _public_create_response(self, job: dict) -> dict:
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "stage": job["stage"],
            "percent": job["percent"],
            "poll_url": job["poll_url"],
            "created_at": job["created_at"],
            "expires_at": job["expires_at"],
        }


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _visual_result_payload(result, *, source_units: list[dict] | None = None) -> dict:
    return {
        "schema_version": VISUAL_RESULT_SCHEMA_VERSION,
        "prompt_version": VISUAL_PROMPT_VERSION,
        "source_ref": result.source_ref,
        "content_sha256": result.content_sha256,
        "locations": list(result.locations),
        "source_units": list(source_units or []),
        "description": result.description,
        "visible_text": list(result.visible_text),
        "layout": result.layout,
        "warnings": list(result.warnings),
        "remote_services_used": True,
        "provider": result.provider,
        "model": result.model,
        "tool_actions": [
            {
                "type": audit.action_type,
                "arguments": dict(audit.arguments),
                "status": audit.status,
                "diagnostic_ref": audit.diagnostic_ref,
                "warnings": list(audit.warnings),
            }
            for audit in result.image_process_audits
        ],
    }


def _document_position_source_ref(
    location: str,
    source_units: list[dict] | None,
) -> dict:
    match = re.match(r"^PPTX slide (\d+),", location)
    if match and source_units:
        native_slide_index = int(match.group(1))
        source_unit = next(
            (
                item
                for item in source_units
                if item.get("native_slide_index") == native_slide_index
            ),
            None,
        )
        if source_unit is not None:
            return {
                "type": "pptx_slide",
                "source_slide_identity": source_unit["source_slide_identity"],
                "native_slide_index": native_slide_index,
                "hidden": source_unit["hidden"],
                "rendered_page_index": source_unit["rendered_page_index"],
                "location": location,
            }
    return {"type": "document_position", "location": location}


def _bind_pptx_markdown_to_source_units(
    markdown: str,
    source_units: list[dict],
) -> str:
    by_index = {item["native_slide_index"]: item for item in source_units}

    def replacement(match: re.Match[str]) -> str:
        native_slide_index = int(match.group(1))
        source_unit = by_index.get(native_slide_index)
        if source_unit is None:
            return match.group(0)
        rendered_page_index = source_unit["rendered_page_index"]
        rendered = "null" if rendered_page_index is None else str(rendered_page_index)
        hidden = str(source_unit["hidden"]).lower()
        return (
            "<!-- Source slide identity: "
            f"{source_unit['source_slide_identity']}; "
            f"native_slide_index: {native_slide_index}; "
            f"hidden: {hidden}; rendered_page_index: {rendered} -->"
        )

    return re.sub(r"<!-- Slide number: (\d+) -->", replacement, markdown)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _retention_delta(retention: str) -> timedelta:
    if retention == "expired":
        return -timedelta(seconds=1)
    if retention == "short":
        return timedelta(hours=6)
    if retention == "long":
        return timedelta(days=30)
    return timedelta(days=7)


def _is_audio_source(source_path: Path, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].lower()
    return media_type.startswith("audio/") or source_path.suffix.lower() in {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }


def _is_video_source(source_path: Path, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].lower()
    return media_type.startswith("video/") or source_path.suffix.lower() in {
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
    }


def _transcript_markdown(transcript: dict) -> str:
    lines = ["# Transcript", ""]
    segments = transcript.get("segments") or []
    if transcript.get("time_aligned") and segments:
        for segment in segments:
            start = _format_time_seconds(segment.get("start_sec"))
            end = _format_time_seconds(segment.get("end_sec"))
            lines.extend(
                [
                    f"## {start} - {end}",
                    "",
                    str(segment.get("text", "")).strip(),
                    "",
                ]
            )
    else:
        lines.append(str(transcript.get("text", "")).strip())
    return "\n".join(lines).rstrip() + "\n"


def _video_markdown(transcript: dict, media_index: list[dict]) -> str:
    lines = [_transcript_markdown(transcript).rstrip(), "", "## Key Frames", ""]
    for media in media_index:
        lines.append(f"- `{media['id']}` at {media['time_seconds']}s: `{media['path']}`")
    return "\n".join(lines).rstrip() + "\n"


def _format_time_seconds(value) -> str:
    seconds = float(value or 0)
    minutes = int(seconds // 60)
    remaining = seconds - minutes * 60
    return f"{minutes:02d}:{remaining:05.2f}"


def _transcript_warnings(transcript: dict) -> list[dict]:
    if transcript["time_aligned"]:
        return []
    return [
        {
            "code": "transcript_not_time_aligned",
            "message": "Local ASR produced whole-text transcript without timestamped segments.",
        }
    ]


def _prefix_video_frame_paths(frame_result: dict, prefix: str) -> dict:
    def prefixed(path: str) -> str:
        return f"{prefix}/{path}"

    media_index = [
        item | {"path": prefixed(item["path"])}
        for item in frame_result.get("media_index", [])
    ]
    timeline = [
        item | {"path": prefixed(item["path"])}
        for item in frame_result.get("timeline", [])
    ]
    artifacts = {
        artifact_id: artifact | {"path": prefixed(artifact["path"])}
        for artifact_id, artifact in frame_result.get("artifacts", {}).items()
    }
    return {
        "media_index": media_index,
        "timeline": timeline,
        "artifacts": artifacts,
    }


def _events_after(events: list[dict], after: str) -> list[dict]:
    for index, event in enumerate(events):
        if event["event_id"] == after:
            return events[index + 1 :]
    return events


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
