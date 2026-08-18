# File2Doc API Examples

These examples use the v1 HTTP API directly. File2Doc accepts offline files, so
callers should download remote attachments first, then upload the local bytes.

## Table of Contents

- [Start Locally](#start-locally)
- [Exact Retrieval Flow](#exact-retrieval-flow)
- [Upload PDF](#upload-pdf)
- [Upload PPTX](#upload-pptx)
- [Upload Audio](#upload-audio)
- [Upload Video](#upload-video)
- [Poll Job Status](#poll-job-status)
- [Read Queue Metrics](#read-queue-metrics)
- [Read Progress Events](#read-progress-events)
- [Read The Manifest](#read-the-manifest)
- [Download Content Markdown](#download-content-markdown)
- [Download Targeted Media](#download-targeted-media)
- [Download The Optional Zip Package](#download-the-optional-zip-package)

Use one of these base URLs:

```bash
# Local development
BASE_URL=http://localhost:8000

# Production. Public gateway injects the internal auth token.
BASE_URL=https://file2doc.solutionsuite.cn
```

Do not ask production users for `FILE2DOC_BEARER_TOKEN`. The production public
gateway accepts user requests at `https://file2doc.solutionsuite.cn` and injects
the internal bearer token when proxying to the private File2Doc service.

Only use an auth header when calling an auth-enabled local service or a private
cluster endpoint directly:

```bash
AUTH_HEADER=(-H "Authorization: Bearer ${FILE2DOC_BEARER_TOKEN}")
```

For the production public gateway, leave `AUTH_HEADER` empty:

```bash
AUTH_HEADER=()
```

## Start Locally

Auth disabled for a trusted development run:

```bash
FILE2DOC_AUTH_ENABLED=false \
FILE2DOC_STORAGE_ROOT=/tmp/file2doc \
uvicorn file2doc.main:app --host localhost --port 8000
```

Auth enabled:

```bash
test -n "${FILE2DOC_BEARER_TOKEN:?Set FILE2DOC_BEARER_TOKEN through the environment or a secret manager}"
FILE2DOC_AUTH_ENABLED=true \
FILE2DOC_STORAGE_ROOT=/data/file2doc \
uvicorn file2doc.main:app --host 0.0.0.0 --port 8000
```

## Exact Retrieval Flow

Agents should retrieve results in this order:

1. Upload the file with `POST /parse-jobs/upload`.
2. Poll `GET /parse-jobs/{job_id}` until `status` is `completed` or
   `completed_with_warnings`.
3. Fetch the manifest with `GET /parse-jobs/{job_id}/result`.
4. Read `content.artifact_id` from the manifest.
5. Download the content with
   `GET /parse-jobs/{job_id}/artifacts/{artifact_id}`.
6. For targeted media, read `media_index` and download the chosen item's
   `artifact_id` through the same artifact endpoint.
7. Download `GET /parse-jobs/{job_id}/package` only when a zip export or full
   debug package is needed.

The manifest is the durable contract. `job.result.content_url` is a convenience
link, but agents should still be able to derive the same URL from
`content.artifact_id`.

## Upload PDF

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./report.pdf;type=application/pdf' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

Create-job responses contain `job_id` and `poll_url`:

```json
{
  "job_id": "job_abc123",
  "status": "queued",
  "stage": "queued",
  "percent": 0,
  "poll_url": "/parse-jobs/job_abc123",
  "created_at": "2026-06-11T00:00:00Z",
  "expires_at": "2026-06-11T01:00:00Z"
}
```

If `poll_url` is relative, prepend `BASE_URL`.

## Upload PPTX

Office files use the same endpoint. PowerPoint decks return Markdown, complete
rendered Page Images, and a `page_index` covering visible and hidden native
slides. Native text, embedded Visual Items, Visual Parse Results, thumbnails,
and Page Images share a stable `source_slide_identity`.

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./deck.pptx;type=application/vnd.openxmlformats-officedocument.presentationml.presentation' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

Example PPTX Source unit:

```json
{
  "source_slide_identity": "pptx-slide-521",
  "source_unit": "slide",
  "source_unit_index": 14,
  "native_slide_index": 14,
  "hidden": false,
  "rendered_page_index": 14,
  "render_binding": {
    "status": "verified",
    "method": "instrumented_render_marker"
  },
  "parse_status": "parsed",
  "page_image_id": "pptx-slide-521-image",
  "thumbnail_id": "pptx-slide-521-thumb",
  "dynamic_fields": [
    {
      "field_type": "datetime1",
      "cached_value": "9/28/2022",
      "cached_value_authority": "non_authoritative",
      "evaluated_value": "08/18/2026",
      "evaluated_value_authority": "canonical_render",
      "authoritative_representation": "evaluated_value"
    }
  ]
}
```

A hidden slide has `hidden: true`, `rendered_page_index: null`, and no
`page_image_id`. If the instrumented renderer cannot prove a one-to-one mapping,
the Parse Job fails with `pptx_slide_binding_unprovable`; consumers must not use
OCR, similarity, or an Agent guess to move text or images between slides.

## Upload Audio

Audio uses the local FunASR path. The service environment must have `ffmpeg`,
`funasr`, `FILE2DOC_LOCAL_ASR_ENGINE=funasr-local`, and
`FILE2DOC_LOCAL_ASR_MODEL_DIR` pointing at a mounted FunASR model directory.

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./meeting.mp3;type=audio/mpeg' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

Audio manifests include `transcript` and transcript artifacts in the
`artifacts` array. Production transcripts are expected to include sentence-level
`segments` with `start_sec` and `end_sec`.

## Upload Video

Video uses local ASR plus frame extraction. Selected frames are listed in
`media_index` and time anchors are listed in `timeline`.

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./demo.mp4;type=video/mp4' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

## Poll Job Status

```bash
curl -sS "$BASE_URL/parse-jobs/job_abc123" "${AUTH_HEADER[@]}"
```

Running jobs expose the current stage and queue state:

```json
{
  "job_id": "job_abc123",
  "status": "running",
  "stage": "parser_started",
  "percent": 30,
  "latest_progress": {
    "stage": "parser_started",
    "percent": 30,
    "message": "Parser started",
    "detail": {},
    "created_at": "2026-06-11T00:00:00Z"
  },
  "queue": {
    "state": "running",
    "position": null
  },
  "result": null,
  "error": null
}
```

Queued jobs expose their current queue position:

```json
{
  "job_id": "job_waiting",
  "status": "queued",
  "stage": "queued",
  "percent": 0,
  "queue": {
    "state": "queued",
    "position": 1
  }
}
```

Completed jobs include result links:

```json
{
  "job_id": "job_abc123",
  "status": "completed",
  "stage": "completed",
  "percent": 100,
  "result": {
    "manifest_url": "/parse-jobs/job_abc123/result",
    "package_url": "/parse-jobs/job_abc123/package",
    "content_artifact_id": "art_content",
    "content_url": "/parse-jobs/job_abc123/artifacts/art_content"
  },
  "error": null
}
```

Empty extraction results are successful jobs with an empty `content.md`. Check
`manifest.parser.empty_result` to distinguish an empty source or silent media
from a non-empty parse:

```json
{
  "job_id": "job_abc123",
  "status": "completed",
  "stage": "completed",
  "percent": 100,
  "result": {
    "manifest_url": "/parse-jobs/job_abc123/result",
    "package_url": "/parse-jobs/job_abc123/package",
    "content_artifact_id": "art_content",
    "content_url": "/parse-jobs/job_abc123/artifacts/art_content"
  },
  "error": null
}
```

Failed jobs are reserved for unsupported input, missing runtime configuration,
parser/runtime exceptions, timeouts, and storage errors. Failed jobs include
`error.code` and do not have a result package:

```json
{
  "job_id": "job_abc123",
  "status": "failed",
  "stage": "failed",
  "percent": 100,
  "result": null,
  "error": {
    "code": "parser_failed",
    "message": "MarkItDown failed: ..."
  }
}
```

For failed jobs, do not retry the result or artifact endpoints for that job.
`GET /parse-jobs/{job_id}/result` returns `409 result_not_ready` when no result
package exists.

## Read Queue Metrics

```bash
curl -sS "$BASE_URL/metrics"
```

```json
{
  "jobs": {
    "queued": 1,
    "running": 2,
    "completed": 42,
    "completed_with_warnings": 0,
    "failed": 3,
    "expired": 10,
    "total": 58,
    "max_concurrent": 2,
    "active_background_tasks": 0,
    "runtime": "durable"
  },
  "work_items": {
    "queue_depth": 1,
    "oldest_eligible_age_seconds": 12.4,
    "active_leases": 2,
    "expired_leases": 0,
    "retries": 1,
    "terminal_failures": 0
  }
}
```

Durable Parse Job status responses also include operator-visible execution
state. `attempt` starts at 1, and `last_heartbeat_at` advances while a worker
holds the lease:

```json
{
  "execution": {
    "work_items": [
      {
        "kind": "text_parse",
        "status": "leased",
        "attempt": 2,
        "max_attempts": 3,
        "worker_id": "file2doc-text-worker-abc:1:text_parse",
        "attempt_status": "active",
        "last_heartbeat_at": "2026-07-27T12:00:10Z",
        "lease_expires_at": "2026-07-27T12:00:40Z"
      }
    ]
  }
}
```

## Read Progress Events

```bash
curl -sS "$BASE_URL/parse-jobs/job_abc123/events" "${AUTH_HEADER[@]}"
```

To continue after a known event:

```bash
curl -sS "$BASE_URL/parse-jobs/job_abc123/events?after=evt_abc123" "${AUTH_HEADER[@]}"
```

## Read The Manifest

```bash
curl -sS "$BASE_URL/parse-jobs/job_abc123/result" "${AUTH_HEADER[@]}"
```

The manifest uses `schema_version = file2doc.parse-result.v1`. `content` is an
object with `artifact_id`, `path`, and `media_type`. `artifacts` is an array, not
a map.

```json
{
  "schema_version": "file2doc.parse-result.v1",
  "job_id": "job_abc123",
  "source": {
    "filename": "report.pdf",
    "content_type": "application/pdf",
    "sha256": "abc...",
    "size_bytes": 123456,
    "path": "source/report.pdf"
  },
  "content": {
    "artifact_id": "art_content",
    "path": "content.md",
    "media_type": "text/markdown; charset=utf-8"
  },
  "media_index": [
    {
      "id": "page-1-image",
      "kind": "page_image",
      "path": "images/pages/page_001.png",
      "artifact_id": "art_page_1",
      "media_type": "image/png",
      "source_ref": {
        "type": "page",
        "page": 1
      },
      "derived": false
    }
  ],
  "artifacts": [
    {
      "artifact_id": "art_content",
      "kind": "content_markdown",
      "path": "content.md",
      "media_type": "text/markdown; charset=utf-8"
    },
    {
      "artifact_id": "art_page_1",
      "kind": "page_image",
      "path": "images/pages/page_001.png",
      "media_type": "image/png"
    }
  ],
  "warnings": []
}
```

## Download Content Markdown

Read the content artifact id from the manifest, then call the artifact endpoint:

```bash
CONTENT_ARTIFACT_ID=art_content
curl -sS "$BASE_URL/parse-jobs/job_abc123/artifacts/$CONTENT_ARTIFACT_ID" \
  "${AUTH_HEADER[@]}" \
  -o content.md
```

Equivalent endpoint shape:

```http
GET /parse-jobs/{job_id}/artifacts/{artifact_id}
```

## Download Targeted Media

For page images, thumbnails, embedded images, or video frames, select a media
item from `media_index`, then download its `artifact_id`.

```bash
MEDIA_ARTIFACT_ID=art_page_1
curl -sS "$BASE_URL/parse-jobs/job_abc123/artifacts/$MEDIA_ARTIFACT_ID" \
  "${AUTH_HEADER[@]}" \
  -o page-1.png
```

In package terms, targeted media retrieval is the `artifacts/media_index` flow:
use the manifest's `media_index` to choose the asset, then use the corresponding
entry in the `artifacts` array for path, kind, and media type if needed.

## Download The Optional Zip Package

```bash
curl -sS "$BASE_URL/parse-jobs/job_abc123/package" \
  "${AUTH_HEADER[@]}" \
  -o result-package.zip
```

Agents should usually prefer the manifest plus targeted artifact downloads. The
zip is useful for inspection, export, reproducing a parsing issue, or archiving a
complete result package.
