from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import tempfile
import threading
import zipfile
from itertools import count
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

from fastapi import HTTPException

from file2doc.audio import AudioParseFailure, AudioParseOptions, parse_audio_transcript
from file2doc.durable.package_builder import (
    PackageFiles,
    build_legacy_package,
    build_video_package,
)
from file2doc.durable.object_store import ObjectStore
from file2doc.durable.repository import (
    ClaimedWorkItem,
    LeaseLostError,
    PostgresJobRepository,
    new_job_id,
)
from file2doc.parsers import ParseFailure, ParseOptions, parse_content_markdown
from file2doc.rendering import AGENT_PAGE_IMAGE_DPI, AGENT_THUMBNAIL_MAX_EDGE
from file2doc.store import SCHEMA_VERSION, _prefix_video_frame_paths
from file2doc.video import (
    VideoFrameExtractionError,
    VideoToolUnavailable,
    extract_video_frames,
)
from file2doc.version import skill_version_payload


@dataclass(frozen=True)
class StoredDownload:
    content: bytes
    media_type: str
    filename: str


class DurableJobStore:
    runtime_name = "durable"
    storage_description = "tos"
    supported_source_groups: ClassVar[list[str]] = [
        "text",
        "pdf",
        "office",
        "image",
        "audio",
        "video",
    ]

    def __init__(
        self,
        repository: PostgresJobRepository,
        object_store: ObjectStore,
    ) -> None:
        self.repository = repository
        self.object_store = object_store

    def create_job(
        self,
        *,
        filename: str,
        content_type: str | None,
        source_bytes: bytes,
        parser_profile: str,
        retention: str,
    ) -> dict[str, Any]:
        safe_filename = Path(filename).name or "source"
        media_type = (
            content_type
            or mimetypes.guess_type(safe_filename)[0]
            or "application/octet-stream"
        )
        work_kinds = _source_work_kinds(safe_filename, media_type)
        job_id = new_job_id()
        source_key = f"jobs/{job_id}/source/{safe_filename}"
        self.object_store.put_bytes(
            source_key,
            source_bytes,
            content_type=media_type,
        )
        try:
            return self.repository.create_parse_job(
                job_id=job_id,
                filename=safe_filename,
                content_type=media_type,
                source_bytes=source_bytes,
                source_object_key=source_key,
                parser_profile=parser_profile,
                retention=retention,
                work_kinds=work_kinds,
            )
        except Exception:
            self.object_store.delete(source_key)
            raise

    def create_job_from_file(
        self,
        *,
        filename: str,
        content_type: str | None,
        source_file: BinaryIO,
        parser_profile: str,
        retention: str,
    ) -> dict[str, Any]:
        safe_filename = Path(filename).name or "source"
        media_type = (
            content_type
            or mimetypes.guess_type(safe_filename)[0]
            or "application/octet-stream"
        )
        digest = hashlib.sha256()
        size_bytes = 0
        source_file.seek(0)
        while chunk := source_file.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
        source_file.seek(0)

        job_id = new_job_id()
        source_key = f"jobs/{job_id}/source/{safe_filename}"
        try:
            self.object_store.put_stream(
                source_key,
                source_file,
                content_type=media_type,
                content_length=size_bytes,
            )
            return self.repository.create_parse_job_from_metadata(
                job_id=job_id,
                filename=safe_filename,
                content_type=media_type,
                source_sha256=digest.hexdigest(),
                source_size_bytes=size_bytes,
                source_object_key=source_key,
                parser_profile=parser_profile,
                retention=retention,
                work_kinds=_source_work_kinds(safe_filename, media_type),
            )
        except Exception:
            self.object_store.delete(source_key)
            raise

    def read_job(self, job_id: str) -> dict[str, Any]:
        return self.repository.read_job(job_id)

    def read_all_jobs(self) -> list[dict[str, Any]]:
        return self.repository.read_all_jobs()

    def runtime_metrics(self) -> dict[str, int | float]:
        return self.repository.read_runtime_metrics()

    def read_events(self, job_id: str, *, after: str | None = None) -> dict[str, Any]:
        return self.repository.read_events(job_id, after=after)

    def read_manifest(self, job_id: str) -> dict[str, Any]:
        self._require_completed(job_id)
        publication = self.repository.read_artifact_record(job_id, "manifest")
        return json.loads(self.object_store.get_bytes(publication["object_key"]))

    def read_artifact(self, job_id: str, artifact_id: str) -> dict[str, Any]:
        self._require_completed(job_id)
        publication = self.repository.read_artifact_record(job_id, artifact_id)
        return publication | {
            "download": StoredDownload(
                content=self.object_store.get_bytes(publication["object_key"]),
                media_type=publication["media_type"],
                filename=Path(publication["path"]).name,
            )
        }

    def build_package_zip(self, job_id: str) -> StoredDownload:
        self._require_completed(job_id)
        publication = self.repository.read_artifact_record(job_id, "package")
        return StoredDownload(
            content=self.object_store.get_bytes(publication["object_key"]),
            media_type="application/zip",
            filename=f"{job_id}.zip",
        )

    def check_readiness(self) -> None:
        self.repository.check_readiness()
        self.object_store.check_readiness()

    def cleanup_expired_jobs(self) -> dict[str, Any]:
        raise HTTPException(
            status_code=501,
            detail={"code": "durable_cleanup_not_implemented"},
        )

    def regenerate_page_image(self, job_id: str, *, page: int, dpi: int) -> dict:
        del job_id, page, dpi
        raise HTTPException(
            status_code=501,
            detail={"code": "durable_page_regeneration_not_implemented"},
        )

    def _require_completed(self, job_id: str) -> dict[str, Any]:
        job = self.read_job(job_id)
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail={"code": "result_expired"})
        if job["status"] not in {"completed", "completed_with_warnings"}:
            raise HTTPException(status_code=409, detail={"code": "result_not_ready"})
        return job


class _LeaseHeartbeat:
    def __init__(
        self,
        repository: PostgresJobRepository,
        claim: ClaimedWorkItem,
        *,
        lease_seconds: int,
        heartbeat_seconds: float,
    ) -> None:
        self.repository = repository
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"file2doc-heartbeat-{claim.work_item_id}",
            daemon=True,
        )

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))

    def ensure_active(self) -> None:
        if self._lost.is_set():
            raise LeaseLostError("Work Item lease heartbeat was lost")
        self.repository.assert_lease_active(self.claim)

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                renewed = self.repository.renew_lease(
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                self._lost.set()
                return
            if not renewed:
                self._lost.set()
                return


class DurableWorker:
    def __init__(
        self,
        repository: PostgresJobRepository,
        object_store: ObjectStore,
        *,
        worker_id: str,
        parse_options: ParseOptions | None = None,
        audio_parse_options: AudioParseOptions | None = None,
        video_frame_extractor=None,
        lease_seconds: int = 300,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.worker_id = worker_id
        self.parse_options = parse_options or ParseOptions.from_env()
        self.audio_parse_options = audio_parse_options
        self.video_frame_extractor = video_frame_extractor or extract_video_frames
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = (
            min(30.0, lease_seconds / 3)
            if heartbeat_seconds is None
            else heartbeat_seconds
        )
        if self.heartbeat_seconds <= 0 or self.heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")

    def run_once(self, kind: str) -> bool:
        claim = self.repository.claim_work_item(
            kind,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return False
        try:
            with _LeaseHeartbeat(
                self.repository,
                claim,
                lease_seconds=self.lease_seconds,
                heartbeat_seconds=self.heartbeat_seconds,
            ) as heartbeat:
                if kind == "text_parse":
                    self._run_text_parse(claim, heartbeat)
                    return True
                if kind == "document_parse":
                    self._run_legacy_parse(claim, heartbeat, include_audio=False)
                    return True
                if kind == "audio_parse":
                    self._run_legacy_parse(claim, heartbeat, include_audio=True)
                    return True
                if kind == "video_asr":
                    self._run_video_asr(claim, heartbeat)
                    return True
                if kind == "video_frames":
                    self._run_video_frames(claim, heartbeat)
                    return True
                if kind == "assembly":
                    self._run_assembly(claim, heartbeat)
                    return True
                self.repository.fail_work_item(
                    claim,
                    code="unsupported_work_item_kind",
                    message=f"Unsupported Work Item kind: {kind}",
                    retryable=False,
                )
        except LeaseLostError:
            return True
        return True

    def _run_text_parse(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        payload = claim.payload
        suffix = Path(payload["filename"]).suffix
        try:
            with tempfile.TemporaryDirectory(prefix="file2doc-work-") as tmpdir:
                source_path = Path(tmpdir) / f"source{suffix}"
                source_path.write_bytes(
                    self.object_store.get_bytes(payload["source_object_key"])
                )
                parsed = parse_content_markdown(
                    source_path,
                    payload["content_type"],
                    self.parse_options,
                )
        except ParseFailure as error:
            self.repository.fail_work_item(
                claim,
                code=error.code,
                message=str(error),
                retryable=False,
            )
            return

        result = {
            "markdown": parsed.markdown,
            "diagnostics": parsed.diagnostics,
            "warnings": parsed.warnings,
        }
        heartbeat.ensure_active()
        result_key = (
            f"jobs/{claim.job_id}/attempts/{claim.lease_token}/parsed.json"
        )
        self.object_store.put_bytes(
            result_key,
            _json_bytes(result),
            content_type="application/json",
        )
        heartbeat.ensure_active()
        self.repository.complete_text_parse(
            claim,
            result_object_key=result_key,
            diagnostics=parsed.diagnostics,
            warnings=parsed.warnings,
        )

    def _run_legacy_parse(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
        *,
        include_audio: bool,
    ) -> None:
        payload = claim.payload
        job = self.repository.read_job(claim.job_id)
        heartbeat.ensure_active()
        package = build_legacy_package(
            job_id=claim.job_id,
            filename=payload["filename"],
            content_type=payload["content_type"],
            source_bytes=self.object_store.get_bytes(payload["source_object_key"]),
            parser_profile=payload["parser_profile"],
            retention=job["retention"],
            expires_at=job["expires_at"],
            parse_options=self.parse_options,
            audio_parse_options=self.audio_parse_options if include_audio else None,
        )
        heartbeat.ensure_active()
        if package.error:
            self.repository.fail_work_item(
                claim,
                code=str(package.error["code"]),
                message=str(package.error["message"]),
                retryable=False,
            )
            return
        descriptor_key = self._stage_package(claim, package, heartbeat)
        self.repository.complete_work_item(
            claim,
            result={
                "package_descriptor_object_key": descriptor_key,
                "warnings": package.warnings,
            },
        )

    def _run_video_asr(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        payload = claim.payload
        suffix = Path(payload["filename"]).suffix
        try:
            with tempfile.TemporaryDirectory(prefix="file2doc-video-asr-") as tmpdir:
                source_path = Path(tmpdir) / f"source{suffix}"
                source_path.write_bytes(
                    self.object_store.get_bytes(payload["source_object_key"])
                )
                transcript = parse_audio_transcript(
                    source_path,
                    payload["content_type"],
                    self.audio_parse_options,
                )
        except AudioParseFailure as error:
            self.repository.complete_work_item(
                claim,
                result={
                    "transcript": None,
                    "error": {"code": error.code, "message": str(error)},
                },
            )
            return
        heartbeat.ensure_active()
        self.repository.complete_work_item(
            claim,
            result={
                "transcript": transcript.to_manifest_transcript(),
                "diagnostics": transcript.diagnostics,
            },
        )

    def _run_video_frames(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        payload = claim.payload
        suffix = Path(payload["filename"]).suffix
        try:
            with tempfile.TemporaryDirectory(prefix="file2doc-video-frames-") as tmpdir:
                source_path = Path(tmpdir) / f"source{suffix}"
                output_dir = Path(tmpdir) / "frames"
                source_path.write_bytes(
                    self.object_store.get_bytes(payload["source_object_key"])
                )
                artifact_numbers = count(1)
                frame_result = self.video_frame_extractor(
                    source_path,
                    output_dir,
                    new_artifact_id=lambda: _attempt_artifact_id(
                        claim,
                        next(artifact_numbers),
                    ),
                )
                frame_result = _prefix_video_frame_paths(
                    frame_result,
                    "images/video_frames",
                )
                files = []
                for artifact in frame_result["artifacts"].values():
                    path = str(artifact["path"])
                    local_path = output_dir / Path(path).name
                    object_key = (
                        f"jobs/{claim.job_id}/attempts/{claim.lease_token}/frames/{Path(path).name}"
                    )
                    self.object_store.put_bytes(
                        object_key,
                        local_path.read_bytes(),
                        content_type=str(artifact["media_type"]),
                    )
                    files.append({"path": path, "object_key": object_key})
                    heartbeat.ensure_active()
        except (VideoToolUnavailable, VideoFrameExtractionError) as error:
            self.repository.complete_work_item(
                claim,
                result={
                    "frames": None,
                    "error": {
                        "code": "video_frame_extraction_failed",
                        "message": str(error),
                    },
                },
            )
            return
        self.repository.complete_work_item(
            claim,
            result={"frames": frame_result, "files": files},
        )

    def _run_assembly(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        work_items = {
            item["kind"]: item
            for item in self.repository.read_work_items(claim.job_id)
            if item["kind"] != "assembly"
        }
        if "video_asr" in work_items or "video_frames" in work_items:
            package = self._assemble_video_package(claim, work_items)
            self._publish_package(claim, heartbeat, package)
            return
        staged_item = next(
            (
                item
                for kind, item in work_items.items()
                if kind in {"document_parse", "audio_parse"}
            ),
            None,
        )
        if staged_item is not None:
            if staged_item["status"] != "completed" or not staged_item["result"]:
                error = staged_item.get("last_error") or {
                    "code": "parse_work_item_failed",
                    "message": "The parser Work Item did not produce a result package",
                }
                self.repository.fail_work_item(
                    claim,
                    code=str(error["code"]),
                    message=str(error["message"]),
                    retryable=False,
                )
                return
            package = self._load_staged_package(
                staged_item["result"]["package_descriptor_object_key"]
            )
            self._publish_package(claim, heartbeat, package)
            return

        job = self.repository.read_job(claim.job_id)
        job.pop("execution", None)
        text_result = work_items["text_parse"]["result"]
        parse_result = json.loads(
            self.object_store.get_bytes(text_result["result_object_key"])
        )
        content = parse_result["markdown"].encode("utf-8")
        content_artifact_id = _content_artifact_id(claim.job_id)
        content_document = {
            "artifact_id": content_artifact_id,
            "kind": "content_markdown",
            "path": "content.md",
            "media_type": "text/markdown; charset=utf-8",
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            **skill_version_payload(),
            "job_id": claim.job_id,
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
            "parser": parse_result["diagnostics"],
            "page_index": [],
            "media_index": [],
            "artifacts": [content_document],
            "warnings": parse_result["warnings"],
            "created_at": _utc_iso(),
            "expires_at": job["expires_at"],
        }
        artifacts_index = {content_artifact_id: content_document}
        manifest_bytes = _json_bytes(manifest, pretty=True)
        artifacts_bytes = _json_bytes(artifacts_index, pretty=True)
        package_bytes = _package_bytes(
            {
                "content.md": content,
                "manifest.json": manifest_bytes,
                "artifacts.json": artifacts_bytes,
            }
        )
        publications = [
            _publication(
                content_artifact_id,
                claim,
                path="content.md",
                content=content,
                media_type="text/markdown; charset=utf-8",
                document=content_document,
            ),
            _publication(
                "manifest",
                claim,
                path="manifest.json",
                content=manifest_bytes,
                media_type="application/json",
                document={"kind": "manifest", "path": "manifest.json"},
            ),
            _publication(
                "package",
                claim,
                path=f"{claim.job_id}.zip",
                content=package_bytes,
                media_type="application/zip",
                document={"kind": "result_package", "path": f"{claim.job_id}.zip"},
            ),
        ]
        heartbeat.ensure_active()
        for publication in publications:
            self.object_store.put_bytes(
                publication["object_key"],
                publication.pop("content"),
                content_type=publication["media_type"],
            )
            heartbeat.ensure_active()

        final_status = (
            "completed_with_warnings"
            if parse_result["warnings"]
            else "completed"
        )
        job.update(
            {
                "status": final_status,
                "stage": final_status,
                "percent": 100,
                "warnings_count": len(parse_result["warnings"]),
                "latest_progress": {
                    "stage": final_status,
                    "percent": 100,
                    "message": "Result package assembled",
                    "detail": {},
                    "created_at": _utc_iso(),
                },
                "result": {
                    "manifest_url": f"/parse-jobs/{claim.job_id}/result",
                    "package_url": f"/parse-jobs/{claim.job_id}/package",
                    "content_artifact_id": content_artifact_id,
                    "content_url": (
                        f"/parse-jobs/{claim.job_id}/artifacts/{content_artifact_id}"
                    ),
                },
            }
        )
        self.repository.complete_assembly(
            claim,
            job_document=job,
            artifacts=publications,
        )

    def _stage_package(
        self,
        claim: ClaimedWorkItem,
        package: PackageFiles,
        heartbeat: _LeaseHeartbeat,
    ) -> str:
        staged_files = []
        for path, content in sorted(package.files.items()):
            object_key = (
                f"jobs/{claim.job_id}/attempts/{claim.lease_token}/staged/{path}"
            )
            media_type = _media_type_for_path(path)
            self.object_store.put_bytes(
                object_key,
                content,
                content_type=media_type,
            )
            staged_files.append(
                {"path": path, "object_key": object_key, "media_type": media_type}
            )
            heartbeat.ensure_active()
        descriptor = {
            "status": package.status,
            "warnings": package.warnings,
            "files": staged_files,
        }
        descriptor_key = (
            f"jobs/{claim.job_id}/attempts/{claim.lease_token}/package.json"
        )
        self.object_store.put_bytes(
            descriptor_key,
            _json_bytes(descriptor),
            content_type="application/json",
        )
        heartbeat.ensure_active()
        return descriptor_key

    def _load_staged_package(self, descriptor_key: str) -> PackageFiles:
        descriptor = json.loads(self.object_store.get_bytes(descriptor_key))
        files = {
            item["path"]: self.object_store.get_bytes(item["object_key"])
            for item in descriptor["files"]
        }
        return PackageFiles(
            files=files,
            status=descriptor["status"],
            warnings=list(descriptor.get("warnings") or []),
        )

    def _assemble_video_package(
        self,
        claim: ClaimedWorkItem,
        work_items: dict[str, dict[str, Any]],
    ) -> PackageFiles:
        job = self.repository.read_job(claim.job_id)
        asr_item = work_items.get("video_asr") or {}
        frame_item = work_items.get("video_frames") or {}
        asr_result = asr_item.get("result") or {}
        frame_work_result = frame_item.get("result") or {}
        warnings = []
        for kind, item, result in (
            ("video_asr", asr_item, asr_result),
            ("video_frames", frame_item, frame_work_result),
        ):
            error = result.get("error") or item.get("last_error")
            if error:
                warnings.append(
                    {
                        "severity": "warning",
                        "code": str(error["code"]),
                        "message": str(error["message"]),
                        "source_ref": {"type": kind},
                    }
                )
        frame_files = {
            item["path"]: self.object_store.get_bytes(item["object_key"])
            for item in frame_work_result.get("files") or []
        }
        return build_video_package(
            job_id=claim.job_id,
            source=job["source"],
            expires_at=job["expires_at"],
            transcript_result=asr_result,
            frame_result=frame_work_result.get("frames"),
            frame_files=frame_files,
            modality_warnings=warnings,
        )

    def _publish_package(
        self,
        claim: ClaimedWorkItem,
        heartbeat: _LeaseHeartbeat,
        package: PackageFiles,
    ) -> None:
        if package.error:
            self.repository.fail_work_item(
                claim,
                code=str(package.error["code"]),
                message=str(package.error["message"]),
                retryable=False,
            )
            return
        files = dict(package.files)
        manifest = json.loads(files["manifest.json"])
        manifest["job_id"] = claim.job_id
        manifest["warnings"] = package.warnings
        files["manifest.json"] = _json_bytes(manifest, pretty=True)
        package_bytes = _package_bytes(files)
        publications = []
        for artifact in manifest["artifacts"]:
            path = str(artifact["path"])
            content = files[path]
            publications.append(
                _publication(
                    str(artifact["artifact_id"]),
                    claim,
                    path=path,
                    content=content,
                    media_type=str(artifact["media_type"]),
                    document=artifact,
                )
            )
        publications.extend(
            [
                _publication(
                    "manifest",
                    claim,
                    path="manifest.json",
                    content=files["manifest.json"],
                    media_type="application/json",
                    document={"kind": "manifest", "path": "manifest.json"},
                ),
                _publication(
                    "package",
                    claim,
                    path=f"{claim.job_id}.zip",
                    content=package_bytes,
                    media_type="application/zip",
                    document={
                        "kind": "result_package",
                        "path": f"{claim.job_id}.zip",
                    },
                ),
            ]
        )
        for publication in publications:
            self.object_store.put_bytes(
                publication["object_key"],
                publication.pop("content"),
                content_type=publication["media_type"],
            )
            heartbeat.ensure_active()

        job = self.repository.read_job(claim.job_id)
        job.pop("execution", None)
        status = package.status
        content_artifact_id = str(manifest["content"]["artifact_id"])
        job.update(
            {
                "status": status,
                "stage": status,
                "percent": 100,
                "warnings_count": len(package.warnings),
                "error": None,
                "latest_progress": {
                    "stage": status,
                    "percent": 100,
                    "message": "Result package assembled",
                    "detail": {},
                    "created_at": _utc_iso(),
                },
                "result": {
                    "manifest_url": f"/parse-jobs/{claim.job_id}/result",
                    "package_url": f"/parse-jobs/{claim.job_id}/package",
                    "content_artifact_id": content_artifact_id,
                    "content_url": (
                        f"/parse-jobs/{claim.job_id}/artifacts/{content_artifact_id}"
                    ),
                },
            }
        )
        self.repository.complete_assembly(
            claim,
            job_document=job,
            artifacts=publications,
        )


def _source_work_kinds(filename: str, content_type: str) -> tuple[str, ...]:
    media_type = content_type.split(";", 1)[0].lower()
    suffix = Path(filename).suffix.lower()
    if media_type.startswith("video/") or suffix in {
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
    }:
        return ("video_asr", "video_frames")
    if media_type.startswith("audio/") or suffix in {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }:
        return ("audio_parse",)
    return ("text_parse",) if _is_text_source(filename, content_type) else ("document_parse",)


def _is_text_source(filename: str, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].lower()
    return media_type.startswith("text/") or Path(filename).suffix.lower() in {
        ".csv",
        ".json",
        ".md",
        ".txt",
    }


def _attempt_artifact_id(claim: ClaimedWorkItem, number: int) -> str:
    digest = hashlib.sha256(
        f"{claim.lease_token}:artifact:{number}".encode()
    ).hexdigest()[:24]
    return f"art_{digest}"


def _media_type_for_path(path: str) -> str:
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _publication(
    artifact_id: str,
    claim: ClaimedWorkItem,
    *,
    path: str,
    content: bytes,
    media_type: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "object_key": (
            f"jobs/{claim.job_id}/attempts/{claim.lease_token}/result-package/{path}"
        ),
        "media_type": media_type,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "document": document,
        "content": content,
    }


def _package_bytes(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    return payload.getvalue()


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
    ).encode("utf-8")


def _content_artifact_id(job_id: str) -> str:
    return f"art_{hashlib.sha256(f'{job_id}:content'.encode()).hexdigest()[:24]}"


def _utc_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
