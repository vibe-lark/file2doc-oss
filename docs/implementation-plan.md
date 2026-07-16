# First Implementation Plan

This plan turns the File2Doc v1 contract into a PDF tracer bullet.

## 1. Project Skeleton

- Create a Python FastAPI service.
- Add configuration for storage root, authentication, resource limits, parser profile defaults, and concurrency.
- Keep the first implementation HTTP-first; do not build a CLI yet.
- Expose FastAPI OpenAPI docs.
- Target pure CPU Kubernetes deployment first. Size the first production-like Pod generously, with persistent storage mounted at `/data/file2doc`.
- Build production images in a controlled CI or release builder so dependency
  and image layer caching are reproducible.
- Preinstall Python, File2Doc, pinned MarkItDown Core, the File2Doc-owned visual plugin, the provider SDK, ffmpeg, and local ASR dependencies in the image.
- Use lightweight module boundaries:
  - `api/` for HTTP routes.
  - `jobs/` for job models, store, and worker.
  - `intake/` for file uploads, optional service-local paths, MIME detection, and source file records.
  - `parsers/` for MarkItDown, File2Doc Visual Parsing, local ASR, and future modality adapters.
  - `rendering/` for page images and thumbnails.
  - `packages/` for manifest, content markdown, and result package writing.
  - `storage/` for local filesystem storage.
  - `errors.py` for shared error codes and typed failures.

## 2. Job API

- `POST /parse-jobs/upload` creates a parse job from a file upload.
- `POST /parse-jobs/service-local-path` optionally creates a parse job from a service-local path when that capability is enabled.
- `GET /parse-jobs/{job_id}` returns status, current progress, warnings, errors, and result availability.
- `GET /parse-jobs/{job_id}/events` returns progress event history as JSON.
- `GET /parse-jobs/{job_id}/result` returns the latest manifest.
- `GET /parse-jobs/{job_id}/package` optionally returns the complete result package as an archive.
- Add artifact download endpoints for `content.md`, manifest revisions, media, OCR, and parser artifacts.
- Download artifacts by artifact id rather than caller-provided paths. Manifest entries should include package-relative paths for offline use and artifact ids for HTTP download.
- Use polling for the first implementation; persist progress events with job metadata.
- Use SQLite for job metadata, progress events, warnings, errors, and expiration state. Result packages remain on the local filesystem under the storage root.
- Start with four SQLite tables: `jobs`, `progress_events`, `warnings`, and `artifacts`.
- Use prefixed identifiers such as `job_...`, `rp_...`, `src_...`, `evt_...`, and `warn_...`.

## 3. Source Intake

- Accept file uploads as the default agent-facing intake path.
- Optionally accept service-local paths for deployment-local, shared-volume, or debugging workflows.
- Document that service-local paths are resolved on the File2Doc service host, not on the caller's machine.
- Normalize both into a source file record.
- Compute `sha256`.
- Detect MIME and source kind from content, not only file extension.
- Enforce resource limits early where possible.

## 4. Document Parser

- Integrate MarkItDown as the primary lightweight document-to-Markdown parser.
- Enable `file2doc-markitdown-visual` through the standard MarkItDown plugin entry point for standalone images, PDF pages, and embedded Office images.
- Disable MarkItDown Azure Document Intelligence, Azure Content Understanding, URL fetching, YouTube transcript fetching, and built-in audio transcription.
- Do not use temporary text-extraction fallbacks for main content markdown.
- Record MarkItDown Core, visual plugin, model endpoint, parser options, and parser errors.
- Treat complete MarkItDown failure as job failure.
- Allow `completed_with_warnings` when some visual items fail but usable native or visual Markdown exists.
- Do not write API keys or visual-provider request payloads to logs, manifests, parser artifacts, or diagnostics.
- Configure Visual Parsing only through `FILE2DOC_VISUAL_MODEL`, `FILE2DOC_VISUAL_API_KEY`, `FILE2DOC_VISUAL_BASE_URL`, `FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS`, `FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS`, `FILE2DOC_VISUAL_MAX_CONCURRENCY`, `FILE2DOC_VISUAL_ARTIFACT_TTL_SECONDS`, `FILE2DOC_VISUAL_ARTIFACT_RELEASE_GRACE_SECONDS`, and `FILE2DOC_VISUAL_ARTIFACT_ALLOWED_HOSTS`. The retired `FILE2DOC_OCR_*` names are not aliases.

## 4a. Audio and Video Parser

- Reuse the local ASR approach from `attachment-to-doc-local` based on `sherpa-onnx` and the Paraformer Chinese model.
- Use ffmpeg for audio extraction and candidate video frame extraction.
- Implement change-aware frame extraction in the first version using lightweight image hashing or similar CPU-friendly frame-difference logic.
- Keep audio/video transcription local; do not use MarkItDown built-in audio transcription.
- Target time-aligned transcripts through VAD or chunking when implemented; if unavailable, return whole-text transcripts with `time_aligned=false`.

## 5. Page Rendering

- Render page images for PDF pages.
- Generate thumbnails with default `thumbnail_max_edge = 512`.
- Use default `agent` profile page image quality: `standard`, 144 DPI.
- Store package-relative media paths.
- Keep PyMuPDF or an equivalent renderer scoped to visual rendering, not main content extraction.

## 6. Result Package Writer

- Write `content.md` in source reading order.
- Add invisible HTML comments containing YAML metadata at page, section, table, image, and important parser-block boundaries.
- Write `manifest.json` with `schema_version = file2doc.parse-result.v1`.
- Create `revisions/manifest-0001.json`.
- Write Media Index, Page Index, warnings, parser versions, parser artifacts, and package expiration.
- Use stable `media:id` references in markdown.

## 7. Progress, Warnings, and Errors

- Emit persisted progress events using stable stages.
- Use `completed_with_warnings` for partial parse results.
- Use clear error codes for hard failures, including parser unavailable, source too large, unsupported type, visual parsing failed, ASR failed, and result expired.
- Include source refs for page-level warnings.

## 8. API Examples

- Create `docs/api-examples.md`.
- Include examples for auth disabled and bearer token auth.
- Cover file upload, optional service-local path submission if enabled, polling, manifest retrieval, content download, and media artifact download.

## 9. Smoke Test

- Run the service locally.
- Submit a representative PDF through upload.
- Optionally, if service-local paths are enabled, submit the same PDF through a path that exists on the File2Doc service host.
- Verify progress events, manifest, content markdown, page images, thumbnails, and parser artifacts.
- Verify parser failure produces a readable failed job.

## Acceptance Criteria

- FastAPI service starts and exposes OpenAPI docs.
- `auth.enabled=false` allows direct calls in trusted proof-of-concept environments.
- Uploaded PDF creates a parse job successfully.
- Job status can be polled with stage and progress information.
- Successful MarkItDown parsing produces a result package.
- `content.md` is generated from MarkItDown output, with all pixel content routed through `file2doc-markitdown-visual` when configured.
- `manifest.json` uses `schema_version = file2doc.parse-result.v1`.
- Media Index includes page images and thumbnails.
- Page Index includes page, source unit, parse status, and media references.
- Parser artifacts are retained under `parser_artifacts/markitdown/` when useful.
- MarkItDown unavailable produces a failed job with a readable error code and message.
- Page or block degradation can produce `completed_with_warnings`.
- API examples cover file upload, optional service-local path submission if enabled, polling, manifest retrieval, content download, and media artifact download.
- Result package paths are package-relative and do not expose service-side absolute paths.
- MarkItDown is the primary document parser.
- Visual Parsing is enabled only through the canonical `FILE2DOC_VISUAL_*` contract.
- Azure, URL, YouTube, and built-in audio transcription paths are disabled.

Optional acceptance:

- If service-local paths are enabled, a service-local path PDF creates a parse job successfully.
