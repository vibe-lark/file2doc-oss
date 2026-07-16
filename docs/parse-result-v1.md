# Parse Result v1 Contract Draft

This draft captures the File2Doc v1 service contract for offline file parsing.

## Scope

File2Doc v1 accepts offline files and produces result packages for downstream agents. It does not create or update Lark cloud documents.

Supported source entry points:

- File uploads over HTTP. This is the general entry point for agents, including agents running on a user's machine or a separate VM.
- Optional service-local paths. These paths are resolved in the File2Doc service environment and are only for deployment-local, shared-volume, or debugging workflows.

Explicitly unsupported in v1:

- Lark document, sheet, base, wiki, or minutes URLs.
- Direct web URL fetching.
- Remote crawler behavior.
- Lark cloud document publishing.

Upstream agents may download remote attachments first, then submit the resulting offline file.

## First Implementation Slice

The first implementation slice should prove the service contract with a PDF tracer bullet:

```text
uploaded PDF
-> parse job
-> progress events
-> MarkItDown Markdown extraction
-> file2doc-markitdown-visual for every eligible pixel item
-> thumbnails and page images
-> OCR layer when available
-> manifest.json and content.md
-> result package retrieval
```

This slice should prioritize the job model, result package structure, manifest v1, progress events, and a usable PDF path before broadening Office, video, audio, Excel, derived assets, or full cache reuse.

The first slice uses MarkItDown as the primary document parser. `file2doc-markitdown-visual` owns standalone images, scanned pages, and embedded images through the configured Ark Responses endpoint. Azure Document Intelligence, Azure Content Understanding, URL fetching, YouTube transcript fetching, and MarkItDown built-in audio transcription remain disabled.

If MarkItDown returns usable Markdown but OCR or media extraction fails for some regions, the job may complete with warnings. If a document produces no usable Markdown and OCR is unavailable or fails, the job should fail visibly.

The first slice must support file uploads. Service-local paths may be implemented as an optional deployment-local entry point, but skills and ordinary agents should use file upload by default. Any service-local path is resolved on the File2Doc service host, not on the caller's machine.

## Implementation Roadmap

After the PDF tracer bullet, modality support should be added in this order:

1. Office documents, with PPT/PPTX and Word/DOC/DOCX prioritized after PDF. Excel/XLSX/XLS/CSV remains lower priority.
2. Video and audio, by adding change-aware frame extraction and local `sherpa-onnx` transcripts.
3. Excel and CSV, by adding table summaries and table artifacts.

For Office documents, the manifest source remains the original Office file. The converted PDF is a parser artifact, not the source.

## Service Shape

The HTTP service is the primary surface. A CLI client may be added later, but it is not required for the first implementation slice. Skills and agents can call the HTTP API directly.

The first implementation should store source files and result packages on the local filesystem under a configured storage root. Manifests should use package-relative paths and APIs should expose download endpoints or artifact references instead of service-side absolute paths, so storage can move to object storage later without changing result package semantics.

Possible future CLI commands:

```bash
file2doc serve
file2doc parse ./report.pdf --wait
file2doc asset page-image --job <job_id> --page 7 --quality high
```

## Authentication

HTTP APIs should support lightweight bearer token authentication, but the first proof-of-concept may explicitly disable authentication in trusted local or internal environments.

```http
Authorization: Bearer <token>
```

Recommended configuration:

- `auth.enabled=false`: allowed for trusted local or internal proof-of-concept deployments.
- `auth.enabled=true`: require `Authorization: Bearer <token>`.

The first implementation does not need multi-tenant users, fine-grained permissions, or audit logs.

## Parse Jobs

Parsing is asynchronous by default.

```http
POST /parse-jobs/upload
POST /parse-jobs/service-local-path
GET /parse-jobs/{job_id}
GET /parse-jobs/{job_id}/events
GET /parse-jobs/{job_id}/result
GET /parse-jobs/{job_id}/package
POST /parse-jobs/{job_id}/assets/page-image
```

`/parse-jobs/upload` is the default agent-facing endpoint. `/parse-jobs/service-local-path` is optional and should only be enabled for deployment-local workflows where the submitted path is accessible from the File2Doc service host.

Upload requests use `multipart/form-data`:

```text
file: binary
parser_profile: agent | fidelity
retention: short | standard | long
options: JSON string, optional
```

Create-job responses return a lightweight job summary, not a manifest:

```json
{
  "job_id": "job_...",
  "status": "queued",
  "stage": "queued",
  "percent": 0,
  "poll_url": "/parse-jobs/job_...",
  "created_at": "...",
  "expires_at": "..."
}
```

Polling responses return current job state, the latest progress event, warning/error summary, and result links when available:

```json
{
  "job_id": "job_...",
  "status": "running",
  "stage": "rendering",
  "percent": 42,
  "latest_progress": {
    "stage": "rendering",
    "percent": 42,
    "message": "Rendering PDF pages",
    "detail": {
      "current_page": 12,
      "page_count": 30
    },
    "created_at": "..."
  },
  "warnings_count": 0,
  "error": null,
  "result": {
    "manifest_url": "/parse-jobs/job_.../result",
    "package_url": "/parse-jobs/job_.../package",
    "content_artifact_id": "art_...",
    "content_url": "/parse-jobs/job_.../artifacts/art_..."
  },
  "expires_at": "..."
}
```

Progress history is available as JSON:

```http
GET /parse-jobs/{job_id}/events?after=evt_...
```

```json
{
  "events": [],
  "next_after": "evt_..."
}
```

`GET /parse-jobs/{job_id}/result` returns the latest manifest JSON directly when the result is ready. If the result is not ready, return `409 result_not_ready`; if the result has expired, return `410 result_expired`.

Final outputs are retrieved through the result package:

1. Poll `GET /parse-jobs/{job_id}` until the job is complete.
2. Read `GET /parse-jobs/{job_id}/result` to get the manifest.
3. Read `content.artifact_id` from the manifest for the main Markdown content.
4. Download individual artifacts through `GET /parse-jobs/{job_id}/artifacts/{artifact_id}`.
5. For targeted media retrieval, choose items from `media_index` and download
   their `artifact_id` values through the same artifact endpoint.

`GET /parse-jobs/{job_id}/package` may return the complete result package as a zip archive for callers that need to export or inspect all artifacts at once. Agents should prefer the manifest plus targeted artifact downloads.

The zip archive may be generated on first request rather than at job completion. Once generated, it can be registered as an artifact and reused until the manifest revision changes or the result package expires.

Small-file CLI workflows may wait for completion, but they still use the same job model.

The first implementation may use polling instead of server-sent events. Progress events should still be persisted with job metadata so clients can reconnect and understand the latest stage.

The first implementation may execute jobs through in-process background workers with a small `max_concurrent_jobs` limit. Job state, progress, warnings, and result package locations should be persisted outside process memory so a completed or failed job remains inspectable after request completion.

Because the first slice does not require a CLI, it should include API documentation and examples instead. FastAPI OpenAPI output should be available, and a companion API examples document should show PDF upload, local-path submission, job polling, manifest retrieval, content download, and media artifact download.

## Job Status

Recommended statuses:

- `queued`
- `running`
- `completed`
- `completed_with_warnings`
- `failed`
- `expired`

Only unreadable, unclassifiable, or wholly unsupported sources should fail the entire job. Partial modality or page failures should produce warnings and a partial parse result when usable artifacts exist.

## Progress Events

Progress events use stable top-level stages and parser-specific details.

Recommended stages:

- `queued`
- `intaking`
- `classifying`
- `extracting`
- `transcribing`
- `rendering`
- `ocr`
- `assembling`
- `completed`
- `failed`

Example:

```json
{
  "stage": "rendering",
  "percent": 42,
  "message": "Rendering PDF pages",
  "detail": {
    "current_page": 12,
    "page_count": 30
  }
}
```

## Result Package

Parse jobs produce result packages, not single JSON responses.

Suggested structure:

```text
result-package/
├── manifest.json
├── content.md
├── revisions/
│   ├── manifest-0001.json
│   └── manifest-0002.json
├── images/
│   ├── pages/
│   ├── frames/
│   ├── embedded/
│   └── thumbs/
├── parser_artifacts/
│   └── markitdown/
├── diagnostics/
├── ocr/
├── transcripts/
└── tables/
```

`manifest.json` always points to the latest manifest revision. Historical revisions are kept for debugging and auditability.

Parser-native outputs may be retained under `parser_artifacts/` for debugging and future remapping, but they are not the downstream contract. Downstream agents should consume `content.md` and the standard manifest indexes by default.

Image Process Zoom/Rotate outputs returned by the visual provider are a narrow exception: File2Doc downloads the actual provider result into `diagnostics/image-process/` and registers it in both `media_index` and `artifacts`. These entries use `kind=image_process_zoom_result` or `kind=image_process_rotate_result`, identify the exact visual input through `source_ref`, and carry `lifecycle=structured_workflow_draft`, `attachment_role=diagnostic_only`, `expires_at`, and `availability`. They are evidence for reviewing a Structured Workflow Draft, not source files or business attachments, and their paths never appear in Content Markdown. If the provider returns no derived image, File2Doc does not create one.

Draft owners release these diagnostics after confirm, cancel, or draft expiry:

```http
POST /parse-jobs/{job_id}/diagnostics/release
```

The endpoint is idempotent and returns `job_id`, `released_count`, `artifact_ids`, `release_expires_at`, and `already_released`. Release shortens each diagnostic expiry to the configured grace window without changing source or content artifacts. Cleanup deletes expired diagnostic bytes but keeps manifest tombstones with `availability=expired` and `expired_at`; later artifact downloads return `410 artifact_expired`. Provider image URLs must use an explicitly allowed HTTPS hostname, resolve only to public addresses, and may not redirect. A provider action whose returned image cannot be retained fails that visual item explicitly and does not fall back to Markdown-only acceptance.

## Manifest Skeleton

```json
{
  "schema_version": "file2doc.parse-result.v1",
  "manifest_revision": 1,
  "result_package_id": "rp_...",
  "job_id": "job_...",
  "status": "completed_with_warnings",
  "created_at": "2026-06-11T00:00:00Z",
  "expires_at": "2026-06-12T00:00:00Z",
  "source": {
    "kind": "uploaded_file",
    "filename": "report.pdf",
    "size_bytes": 1234567,
    "content_type": "application/pdf",
    "sha256": "...",
    "path": "source/report.pdf"
  },
  "parser_profile": "agent",
  "parser_versions": {
    "file2doc": "0.1.0",
    "markitdown": "unknown",
    "file2doc_markitdown_visual": "unknown",
    "asr": "unknown"
  },
  "language": {
    "detected": "zh-CN",
    "confidence": 0.82,
    "source": "parser"
  },
  "options": {
    "page_image_quality": "standard",
    "page_image_dpi": 144,
    "thumbnail_max_edge": 512
  },
  "content": {
    "artifact_id": "art_content",
    "path": "content.md",
    "media_type": "text/markdown; charset=utf-8"
  },
  "page_index": [],
  "media_index": [],
  "timeline": [],
  "transcript": null,
  "artifacts": [
    {
      "artifact_id": "art_content",
      "kind": "content_markdown",
      "path": "content.md",
      "media_type": "text/markdown; charset=utf-8"
    }
  ],
  "warnings": []
}
```

In the current public manifest, `content` is an object with `artifact_id`,
`path`, and `media_type`. `artifacts` is an array of artifact objects, not a map.
Agents should use `content.artifact_id` for Markdown and `media_index[*].artifact_id`
for targeted visual media.

## Content Markdown

`content.md` is the clean, agent-readable body. It should prioritize understanding and rearrangement over source fidelity.

Markdown, JSON, and text artifacts should be encoded as UTF-8 without BOM. Invalid source characters should be replaced locally and reported with warnings instead of failing the entire job, unless the main content cannot be parsed.

File2Doc should treat MarkItDown output as the v1 Markdown source of truth. Parser-native logs or intermediate files may be retained in parser artifacts for debugging, but the v1 contract does not expose parser-specific block schemas or bounding boxes.

Rules:

- Prefer MarkItDown Markdown output for supported document formats.
- Preserve the source document reading order.
- Do not summarize, rewrite, or semantically reorganize source content.
- Only emit a top-level title when the source parser provides an explicit document title.
- Visible text produced by File2Doc Visual Parsing may appear in `content.md` where the visual plugin inserts it; model and plugin provenance remains recorded in parser diagnostics.
- Refer to media by stable media references, not file paths.
- Use invisible HTML comments containing YAML for lightweight block metadata when useful.
- Add block metadata only at coarse boundaries such as source pages, slides, frames, or artifacts when File2Doc can determine them without parser-specific layout data.

Example:

```md
# Quarterly Review

<!--
source:
  page: 1
  media_refs:
    - page-1-image
-->

![Page 1 overview](media:page-1-image)
```

## Media Index

The media index is the unified catalog of visual assets.

```json
{
  "id": "page-3-image",
  "kind": "page_image",
  "path": "images/pages/page_003.png",
  "artifact_id": "art_...",
  "thumbnail_path": "images/thumbs/page_003.jpg",
  "thumbnail_artifact_id": "art_...",
  "source_ref": {
    "type": "page",
    "page": 3
  },
  "derived": false
}
```

Media kinds include:

- `page_image`
- `thumbnail`
- `embedded_image`
- `video_frame`
- `source_image`

When File2Doc can extract embedded images from a document parser, those images should become `embedded_image` media items and may be referenced in `content.md` using stable media references. When embedded image extraction is not available, page images and thumbnails still provide visual fallback assets.

## Page Index

Pages link source page understanding to media and OCR layers.

```json
{
  "page": 3,
  "source_unit": "page",
  "source_unit_index": 3,
  "parse_status": "parsed",
  "page_image_id": "page-3-image",
  "thumbnail_id": "page-3-thumb",
  "ocr_layer": {
    "source": "file2doc_markitdown_visual",
    "path": "ocr/page_003.json",
    "remote": true
  }
}
```

If MarkItDown cannot produce text for a page but rendering succeeds, use `parse_status = "visual_only"` and attach a warning.

## OCR Layer

OCR layers remain page-level compatibility assets in the result-package contract. When visible text comes from File2Doc Visual Parsing, layers record `remote=true`, the model endpoint id, and `file2doc-markitdown-visual` provenance.

OCR layers should include text when available. Detailed layout blocks are optional and should not be fabricated when the parser does not provide them.

## Video Frames

Video uses change-aware frame extraction by default. Fixed interval frames are fallback anchors.

Default first-version parameters:

```yaml
candidate_interval_sec: 2
min_keep_interval_sec: 8
fallback_interval_sec: 60
hash_diff_threshold: 8
thumbnail_max_edge: 512
max_video_frames: 300
```

```json
{
  "id": "frame-12",
  "timestamp_sec": 128.4,
  "media_id": "frame-12-image",
  "thumbnail_id": "frame-12-thumb",
  "selection_reason": "scene_change"
}
```

Selection reasons include:

- `first_frame`
- `scene_change`
- `visual_change`
- `interval_fallback`

## Transcript

The manifest targets time-aligned transcripts. Engines that cannot provide segments may return whole-text transcripts with `time_aligned = false`.

Video and audio support should prefer VAD or chunk-based local ASR so transcript segments have usable `start_sec` and `end_sec` values. If only whole-text ASR is available, the manifest should set `time_aligned = false` and include a warning.

```json
{
  "text_path": "transcripts/transcript.txt",
  "segments_path": "transcripts/segments.json",
  "time_aligned": true,
  "time_alignment_method": "vad_chunking",
  "engine": "local-asr",
  "segments": [
    {
      "start_sec": 12.4,
      "end_sec": 18.9,
      "text": "..."
    }
  ]
}
```

## Timeline Index

Video and audio results should include a timeline index when time-based artifacts are available. For video, timeline entries connect transcript segments with nearby selected frames. For audio-only sources, timeline entries may contain transcript segment references without frames.

```json
{
  "start_sec": 12.4,
  "end_sec": 18.9,
  "transcript_segment_ids": ["seg-3"],
  "frame_ids": ["frame-2"],
  "media_refs": ["frame-2-image"]
}
```

## Tables

Large tables should not be fully dumped into `content.md` by default.

`content.md` should include sheet summaries, column names, row counts, previews, and notable statistics. Full table artifacts live under `tables/`.

Document tables follow the same rule when File2Doc can identify them separately from MarkItDown output. Otherwise, table content remains in `content.md` as produced by MarkItDown.

Formula and code content should remain as produced by MarkItDown. File2Doc should not explain or rewrite formula or code content.

Page headers and footers should remain as produced by MarkItDown unless a future parser provides reliable structure for removing or indexing them.

Annotations should remain as produced by MarkItDown unless a future parser provides reliable annotation structure.

Footnotes should remain as produced by MarkItDown.

If MarkItDown produces usable Markdown with localized conversion issues, the job may complete with warnings. If no usable Markdown can be produced, the job should fail unless a modality-specific parser such as local ASR can produce another valid content artifact.

```json
{
  "id": "sheet-1",
  "name": "Revenue",
  "rows": 12000,
  "columns": 14,
  "preview_rows": 20,
  "values_artifact_path": "tables/sheet-1.csv",
  "formulas_artifact_path": "tables/sheet-1-formulas.json",
  "merged_cells": ["A1:C1"],
  "sheet_visibility": "visible"
}
```

Small tables may be inlined when under configured row and character limits.

## Parser Profiles

Built-in profiles:

- `agent`: default, optimized for fast agent understanding and controlled context size.
- `fidelity`: optimized for review and high-fidelity downstream layout.

Default `agent` parameters:

```json
{
  "page_image_quality": "standard",
  "page_image_dpi": 144,
  "thumbnail_max_edge": 512
}
```

Page image quality is primarily expressed as service-defined tiers:

- `low`: 96 DPI
- `standard`: 144 DPI
- `high`: 216 or 288 DPI

Advanced DPI override may be supported within service-configured bounds.

## Derived Assets

Agents may request higher-quality page images after the initial parse.

```http
POST /parse-jobs/{job_id}/assets/page-image
```

Example request:

```json
{
  "page": 7,
  "quality": "high"
}
```

The service should not rerun full parsing for derived page images. It should reuse the retained source file or intermediate PDF, add new media items, and create a manifest revision.

If the source file has expired, return `source_expired`.

## Cache and Retention

Each source file has content identity based on `sha256`, but job and result identifiers remain separate.

Cache keys must include:

```text
source_sha256 + parser_profile + parser_version + options_hash
```

Suggested retention tiers:

- `short`: 1 hour
- `standard`: 24 hours
- `long`: 7 days

The service controls the real maximum retention. Manifests and job responses should expose `expires_at`.

## Resource Limits

The service should define and report resource limits.

Recommended limits:

- `max_upload_size_mb`
- `max_page_count`
- `max_video_duration_sec`
- `max_audio_duration_sec`
- `max_sheet_rows_preview`
- `max_output_package_mb`
- `max_concurrent_jobs`
- `max_derived_asset_requests`

Hard limit violations fail the job with a clear error code. Soft limit handling should produce warnings and explain truncation, downsampling, or skipped artifacts.

Suggested proof-of-concept defaults:

```yaml
max_upload_size_mb: 512
max_page_count: 500
max_video_duration_sec: 14400
max_audio_duration_sec: 14400
max_sheet_rows_preview: 100
max_output_package_mb: 2048
max_concurrent_jobs: 1
max_derived_asset_requests_per_job: 50
```

All limits should be configurable by deployment.

## Warnings

Warnings are non-fatal parsing problems.

```json
{
  "severity": "warning",
  "code": "page_visual_only",
  "message": "OCR failed on page 18; page image and thumbnail are available.",
  "source_ref": {
    "type": "page",
    "page": 18
  }
}
```

Warning severities:

- `info`: useful detail that does not affect result use.
- `warning`: usable result with local incompleteness or truncation.
- `error`: local failure while the job can still produce a partial result.

## Error Codes

First-version error codes:

- `unsupported_source_kind`
- `empty_parse_result`
- `source_not_found`
- `source_too_large`
- `source_unreadable`
- `mime_detection_failed`
- `resource_limit_exceeded`
- `parser_unavailable`
- `parser_parse_failed`
- `visual_item_failed`
- `asr_failed`
- `rendering_failed`
- `thumbnail_generation_failed`
- `manifest_write_failed`
- `result_expired`
- `auth_required`
- `auth_failed`
- `internal_error`

Top-level job errors should include `code`, `message`, and `stage`. Messages are for humans and agents; raw stderr or stack traces should be stored as diagnostics rather than returned as primary messages.

## Diagnostics

Parser stderr, stack traces, and tool logs may be stored under `diagnostics/` for explicit debugging. Normal job responses should return clean errors and warnings instead of raw logs. Diagnostics should be marked debug-only in the manifest and should avoid exposing sensitive environment values.

## Remote and Local Processing

File2Doc v1 uses MarkItDown for local native-text conversion and a File2Doc-owned visual boundary for pixel interpretation. Visual requests may send standalone images, extracted embedded images, or rendered pages to the configured Ark Responses endpoint.

Azure Document Intelligence, Azure Content Understanding, URL fetching, YouTube transcript fetching, and MarkItDown built-in audio transcription are disabled in v1. Audio and video transcription should use the local `sherpa-onnx` ASR path inherited from `attachment-to-doc-local`.
