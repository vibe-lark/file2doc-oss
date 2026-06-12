---
name: file2doc-http
description: Use File2Doc HTTP API to parse offline files and retrieve result package artifacts for agents.
---

# File2Doc HTTP Skill

Use this skill when an agent needs to turn a local PDF, PPTX, Office document,
audio file, or video file into Markdown and downloadable media artifacts through
File2Doc. The CLI is deferred; call HTTP directly.

## Inputs

- `BASE_URL`: local `http://127.0.0.1:8000` or your deployed File2Doc base URL,
  such as `https://file2doc.example.com`.
- `FILE2DOC_BEARER_TOKEN`: required only when the deployment has auth enabled.
- Local file bytes. Download remote files yourself before calling File2Doc.

## Retrieval Flow

1. Upload with `POST /parse-jobs/upload`.
2. Poll `GET /parse-jobs/{job_id}` until `status` is `completed`,
   `completed_with_warnings`, or `failed`.
3. If failed, inspect `error.code`. For `empty_parse_result`, stop artifact
   retrieval because there is no result package.
4. Fetch the manifest with `GET /parse-jobs/{job_id}/result`.
5. Read `content.artifact_id`.
6. Download Markdown from
   `/parse-jobs/{job_id}/artifacts/{artifact_id}`.
7. For targeted media, use the `artifacts/media_index` flow: choose an item from
   `media_index`, read its `artifact_id`, then download that artifact endpoint.
8. Use `/parse-jobs/{job_id}/package` only when a full zip package is needed.

## HTTP Calls

```bash
BASE_URL=${BASE_URL:-http://127.0.0.1:8000}
AUTH_HEADER=()
if [ -n "${FILE2DOC_BEARER_TOKEN:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${FILE2DOC_BEARER_TOKEN}")
fi

curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  "${AUTH_HEADER[@]}" \
  -F 'file=@./report.pdf;type=application/pdf' \
  -F 'parser_profile=agent' \
  -F 'retention=short'

curl -sS "$BASE_URL/parse-jobs/job_abc123" "${AUTH_HEADER[@]}"
curl -sS "$BASE_URL/parse-jobs/job_abc123/result" "${AUTH_HEADER[@]}"
curl -sS "$BASE_URL/parse-jobs/job_abc123/artifacts/art_content" "${AUTH_HEADER[@]}"
```

## Manifest Contract

Trust the manifest over guessed paths:

- `content` is an object with `artifact_id`, `path`, and `media_type`.
- `artifacts` is an array of artifact objects, not a map.
- `media_index` lists page images, thumbnails, embedded images, and video frames.
- Artifact downloads always use
  `/parse-jobs/{job_id}/artifacts/{artifact_id}`.

Minimal shape:

```json
{
  "schema_version": "file2doc.parse-result.v1",
  "job_id": "job_abc123",
  "content": {
    "artifact_id": "art_content",
    "path": "content.md",
    "media_type": "text/markdown; charset=utf-8"
  },
  "media_index": [],
  "artifacts": [
    {
      "artifact_id": "art_content",
      "kind": "content_markdown",
      "path": "content.md",
      "media_type": "text/markdown; charset=utf-8"
    }
  ]
}
```

## Modality Notes

- PDF: expect Markdown content and, when rendering succeeds, page images and
  thumbnails in `media_index`.
- PPTX and Office: expect Markdown content from MarkItDown; visual assets depend
  on available conversion/rendering support.
- Audio: expect Markdown content plus `transcript` and transcript artifacts.
- Video: expect Markdown content, `transcript`, selected frames in `media_index`,
  and time anchors in `timeline`.

## Failure Handling

If polling returns:

```json
{
  "status": "failed",
  "result": null,
  "error": {
    "code": "empty_parse_result",
    "message": "Parser returned no usable Markdown content."
  }
}
```

Do not call result, content, media, or package endpoints for that job. Report the
parse failure and ask for a better source file or explicit fallback path.
