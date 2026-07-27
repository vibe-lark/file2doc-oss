from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from file2doc.audio import AudioParseOptions
from file2doc.parsers import ParseOptions
from file2doc.store import JobStore, SCHEMA_VERSION, _transcript_warnings, _video_markdown
from file2doc.version import skill_version_payload


@dataclass(frozen=True)
class PackageFiles:
    files: dict[str, bytes]
    status: str
    warnings: list[dict[str, Any]]
    error: dict[str, Any] | None = None


def build_legacy_package(
    *,
    job_id: str,
    filename: str,
    content_type: str,
    source_bytes: bytes,
    parser_profile: str,
    retention: str,
    expires_at: str,
    parse_options: ParseOptions,
    audio_parse_options: AudioParseOptions | None,
) -> PackageFiles:
    with tempfile.TemporaryDirectory(prefix="file2doc-durable-legacy-") as tmpdir:
        store = JobStore(
            Path(tmpdir),
            parse_options=parse_options,
            audio_parse_options=audio_parse_options,
        )
        created = store.create_job(
            filename=filename,
            content_type=content_type,
            source_bytes=source_bytes,
            parser_profile=parser_profile,
            retention=retention,
        )
        store.complete_job(created["job_id"])
        local_job = store.read_job(created["job_id"])
        if local_job["status"] == "failed":
            return PackageFiles({}, "failed", [], local_job["error"])

        result_root = store.jobs_root / created["job_id"] / "result-package"
        files = {
            path.relative_to(result_root).as_posix(): path.read_bytes()
            for path in sorted(result_root.rglob("*"))
            if path.is_file()
        }
        manifest = json.loads(files["manifest.json"])
        manifest["job_id"] = job_id
        manifest["expires_at"] = expires_at
        files["manifest.json"] = _json_bytes(manifest, pretty=True)
        return PackageFiles(
            files,
            local_job["status"],
            list(manifest.get("warnings") or []),
        )


def build_video_package(
    *,
    job_id: str,
    source: dict[str, Any],
    expires_at: str,
    transcript_result: dict[str, Any] | None,
    frame_result: dict[str, Any] | None,
    frame_files: dict[str, bytes],
    modality_warnings: list[dict[str, Any]],
) -> PackageFiles:
    transcript = _empty_transcript()
    parser_diagnostics: dict[str, Any] = {"empty_result": True}
    engine = "funasr-local"
    if transcript_result and transcript_result.get("transcript") is not None:
        transcript = dict(transcript_result["transcript"])
        parser_diagnostics = dict(transcript_result.get("diagnostics") or {})
        engine = str(transcript.get("engine") or engine)

    media_index = list((frame_result or {}).get("media_index") or [])
    timeline = list((frame_result or {}).get("timeline") or [])
    frame_artifacts = dict((frame_result or {}).get("artifacts") or {})
    usable_transcript = bool(str(transcript.get("text") or "").strip())
    if not media_index and not usable_transcript:
        return PackageFiles(
            {},
            "failed",
            modality_warnings,
            {
                "code": "no_usable_media_result",
                "message": "Neither transcript nor video frames could be produced",
            },
        )

    content_artifact_id = _stable_artifact_id(job_id, "content.md")
    transcript_text_id = _stable_artifact_id(job_id, "transcripts/transcript.txt")
    transcript_segments_id = _stable_artifact_id(job_id, "transcripts/segments.json")
    transcript = transcript | {
        "text_path": "transcripts/transcript.txt",
        "segments_path": "transcripts/segments.json",
    }
    artifacts = {
        content_artifact_id: {
            "artifact_id": content_artifact_id,
            "kind": "content_markdown",
            "path": "content.md",
            "media_type": "text/markdown; charset=utf-8",
        },
        transcript_text_id: {
            "artifact_id": transcript_text_id,
            "kind": "transcript_text",
            "path": "transcripts/transcript.txt",
            "media_type": "text/plain; charset=utf-8",
        },
        transcript_segments_id: {
            "artifact_id": transcript_segments_id,
            "kind": "transcript_segments",
            "path": "transcripts/segments.json",
            "media_type": "application/json; charset=utf-8",
        },
        **frame_artifacts,
    }
    warnings = [*_transcript_warnings(transcript), *modality_warnings]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        **skill_version_payload(),
        "job_id": job_id,
        "source": source,
        "options": {},
        "content": artifacts[content_artifact_id],
        "parser": {
            "name": "local-asr+ffmpeg",
            "engine": engine,
            **parser_diagnostics,
        },
        "transcript": transcript,
        "page_index": [],
        "media_index": media_index,
        "timeline": timeline,
        "artifacts": list(artifacts.values()),
        "warnings": warnings,
        "expires_at": expires_at,
    }
    files = {
        "content.md": _video_markdown(transcript, media_index).encode("utf-8"),
        "transcripts/transcript.txt": str(transcript.get("text") or "").encode("utf-8"),
        "transcripts/segments.json": _json_bytes(transcript.get("segments") or [], pretty=True),
        "manifest.json": _json_bytes(manifest, pretty=True),
        "artifacts.json": _json_bytes(artifacts, pretty=True),
        **frame_files,
    }
    status = "completed_with_warnings" if warnings else "completed"
    return PackageFiles(files, status, warnings)


def _empty_transcript() -> dict[str, Any]:
    return {
        "text": "",
        "time_aligned": False,
        "time_alignment_method": "funasr_sentence_timestamp",
        "engine": "funasr-local",
        "segments": [],
    }


def _stable_artifact_id(job_id: str, path: str) -> str:
    import hashlib

    digest = hashlib.sha256(f"{job_id}:{path}".encode()).hexdigest()[:24]
    return f"art_{digest}"


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
    ).encode("utf-8")
