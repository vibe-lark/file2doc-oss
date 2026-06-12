# File2Doc API Examples

These examples use the v1 HTTP API directly. File2Doc accepts offline files, so
callers should download remote attachments first, then upload the local bytes.

Use one of these base URLs:

```bash
# Local development
BASE_URL=http://127.0.0.1:8000

# Example production deployment. Requires the deployed auth token.
BASE_URL=https://file2doc.example.com
```

Do not put bearer tokens in docs, source files, or shared logs. Pass them through
the environment when auth is enabled:

```bash
AUTH_HEADER=(-H "Authorization: Bearer ${FILE2DOC_BEARER_TOKEN}")
```

## Start Locally

Auth disabled for a trusted development run:

```bash
FILE2DOC_AUTH_ENABLED=false \
FILE2DOC_STORAGE_ROOT=/tmp/file2doc \
uvicorn file2doc.main:app --host 127.0.0.1 --port 8000
```

Auth enabled:

```bash
FILE2DOC_AUTH_ENABLED=true \
FILE2DOC_BEARER_TOKEN='replace-me' \
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

Office files use the same endpoint. PowerPoint decks return Markdown content
from MarkItDown and may include visual assets when the parser or renderer can
produce them.

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./deck.pptx;type=application/vnd.openxmlformats-officedocument.presentationml.presentation' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

## Upload Audio

Audio uses the local ASR path. The service environment must have `ffmpeg`,
`sherpa-onnx`, and `FILE2DOC_LOCAL_ASR_MODEL_DIR` pointing at a mounted model
directory containing `model.int8.onnx` and `tokens.txt`.

```bash
curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./meeting.mp3;type=audio/mpeg' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

Audio manifests include `transcript` and transcript artifacts in the
`artifacts` array.

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

Failed jobs include `error.code` and do not have a result package:

```json
{
  "job_id": "job_abc123",
  "status": "failed",
  "stage": "failed",
  "percent": 100,
  "result": null,
  "error": {
    "code": "empty_parse_result",
    "message": "Parser returned no usable Markdown content."
  }
}
```

For `empty_parse_result`, do not retry the result or artifact endpoints for that
job. Report the file as unparseable, ask for a different source when useful, or
fall back to another user-approved workflow. `GET /parse-jobs/{job_id}/result`
returns `409 result_not_ready` when no result package exists.

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
