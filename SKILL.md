---
name: file2doc-http
description: Parse offline files with the File2Doc HTTP API and retrieve Markdown, manifest, media artifacts, transcripts, and video frames. Use when the user provides local PDFs, Office files, audio, or video and wants agent-readable document content or artifacts.
metadata:
  version: "0.1.23"
---

# File2Doc HTTP

## Quick Start

Use File2Doc for local files only. Download remote attachments first, then upload
the local file bytes.

```bash
BASE_URL=${BASE_URL:-https://file2doc.solutionsuite.cn}
SKILL_VERSION=0.1.23

curl -fsS "$BASE_URL/skills/file2doc-http/version.json?installed_version=$SKILL_VERSION"

curl -sS -X POST "$BASE_URL/parse-jobs/upload" \
  -H "X-File2Doc-Skill-Version: $SKILL_VERSION" \
  -F 'file=@./report.pdf;type=application/pdf' \
  -F 'parser_profile=agent' \
  -F 'retention=short'
```

For production, do not ask users for `FILE2DOC_BEARER_TOKEN`. The public
gateway injects the internal bearer token before proxying to the private
File2Doc service. For local development, set `BASE_URL=http://127.0.0.1:8000`;
only include `Authorization: Bearer ...` when the local service is explicitly
started with auth enabled.

## Workflow

1. Check `/skills/file2doc-http/version.json` once at the start of each task. If
   `update_required` is true, show the supplied update command and stop. If only
   `update_available` is true, mention it without blocking the task.
2. Upload with `POST /parse-jobs/upload` and send
   `X-File2Doc-Skill-Version: 0.1.23` on upload, status, and result requests.
3. Poll `GET /parse-jobs/{job_id}` until `completed`,
   `completed_with_warnings`, or `failed`.
4. If failed, read `error.code` and stop result retrieval because no package
   exists.
5. Fetch the manifest with `GET /parse-jobs/{job_id}/result`.
6. Read `content.artifact_id`.
7. Download Markdown with
   `GET /parse-jobs/{job_id}/artifacts/{artifact_id}`.
8. For media, choose items from `media_index` and download each item's
   `artifact_id` through the same artifact endpoint.
9. Use `GET /parse-jobs/{job_id}/package` only for full zip export or debugging.

## Final Document

Apply this section only when the user asks for a final document, tutorial, SOP,
report, or Lark document. If the user only asks to parse a file or retrieve
artifacts, return the requested results without forcing document creation.

When the target is a Lark document, use the available `lark-doc` or equivalent
document skill for creation, updates, media insertion, and platform-specific
formatting. File2Doc supplies the source content and artifacts; these rules
define the expected document quality.

### Writing and Structure

1. Read the Markdown and manifest before writing. For visual inputs, inspect
   thumbnails, page images, timeline entries, or video frames before choosing
   the document structure.
2. Rewrite for the user's goal. Do not paste raw parser output unchanged.
   Remove repeated headers and footers, broken lines, extraction noise, and
   duplicated content.
3. Use one document title, H2 for main sections, and H3 only when a section
   genuinely needs subdivision. Use paragraphs for explanation, lists for
   procedures, and tables only for genuinely comparable data.
4. Start with a short overview, present the useful content in a task-appropriate
   order, and end with the source references.

### Visuals

- Use images that explain an operation, provide evidence, or clarify a state.
  Do not insert images only for decoration.
- For SOPs and tutorials, each major workflow should include at least one useful source image
  when one is available.
- Put each image immediately after the paragraph or step it supports. Do not
  collect all images at the end of the document.
- Add a short caption describing what the image shows. Preserve page numbers
  for document images and timestamps for video frames.
- Use thumbnails to select images. Insert page images or higher-resolution
  artifacts in the final document when readability matters.
- Do not insert every available page image or video frame. Audio and other
  sources without useful visual artifacts do not require images.

### Source Attribution

Every final document created from File2Doc output must include a concise
`Sources` or `来源` section that lets the user trace every input.

- For a URL input, include the clickable original URL, not a downloaded copy's
  temporary path.
- For a file or attachment, preserve the original filename and attach the original file
  to the final document, or include a user-accessible link to
  its source message, Drive item, or other durable location.
- List every input separately when the result combines multiple sources.
- A local path is not a traceable source. File2Doc artifact URLs, job URLs, and
  generated Markdown are derived outputs and must not replace the original
  source reference.
- If the Agent cannot attach the file or produce a user-accessible source link,
  ask the user for a durable source location before finalizing the document.

### Delivery Check

Before delivery, verify that:

- the document answers the user's actual goal;
- headings, paragraphs, procedures, and tables have been intentionally organized;
- useful images appear beside the content they support;
- obvious broken lines, duplicates, and extraction noise have been removed;
- incomplete parsing has not been filled with invented facts;
- warnings, missing content, and material recognition uncertainty are disclosed;
- every source is traceable by the user.

## Contract

Trust the manifest over guessed paths.

- `content` points to the primary Markdown artifact.
- `artifacts` is an array, not a map.
- `media_index` lists page images, thumbnails, embedded images, and video frames.
- `timeline` contains video frame time anchors.
- `transcript.segments` is the structured ASR output for audio and video.
- Empty parser, OCR, or ASR output is a completed result with empty artifacts
  and `parser.empty_result: true`, not a failed job.
- In production, ASR uses local FunASR and should emit sentence-level
  `start_sec`, `end_sec`, and `text`.
- Prefer `transcripts/segments.json` over parsing transcript text from
  `content.md`.

## Modality Notes

- PDF: use Markdown plus page images or thumbnails when available.
- Office: use Markdown; visual assets depend on parser support.
- Audio: use Markdown plus `transcript` artifacts.
- Video: use transcript, `timeline`, and selected `video_frame` artifacts.
  Frame extraction scans time coverage and scene changes, filters low-information
  or blurry candidates, and removes perceptual near-duplicates. The output count
  follows visual changes and is not capped at a fixed 48.

## Install And Update

Install from the repository so the skills manager can update it:

```bash
npx skills add vibe-lark/file2doc-skill --skill file2doc-http
```

For mainland or sandbox environments that cannot reach GitHub, install from the
File2Doc public gateway:

```bash
curl -fsSL https://file2doc.solutionsuite.cn/skills/file2doc-http.tar.gz -o /tmp/file2doc-http.tar.gz
mkdir -p /tmp/file2doc-skill
tar -xzf /tmp/file2doc-http.tar.gz -C /tmp/file2doc-skill
npx skills add /tmp/file2doc-skill --skill file2doc-http --copy -y
```

Or use the hosted installer:

```bash
curl -fsSL https://file2doc.solutionsuite.cn/skills/file2doc-http/install.sh | sh
```

Update an installed copy from GitHub:

```bash
npx skills update file2doc-http
```

For gateway-installed copies, update by re-running the gateway install command.

## Reference

For full request examples, response shapes, and package retrieval details, see
`docs/api-examples.md`.
