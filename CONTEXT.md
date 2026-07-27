# File2Doc

File2Doc turns offline files into intermediate parse results that downstream agents can use to create polished documents.

## Language

**File Parsing Service**:
A service boundary that accepts offline files and returns parse results for downstream agents. It does not create or update Lark cloud documents.
_Avoid_: Attachment-to-doc service, document publishing service

**CLI Client**:
A command-line entry point that lets local agents and developers submit files, wait for parse jobs, and request derived assets through the same service boundary.
_Avoid_: Script wrapper, local mode

**Parse Result**:
The intermediate output produced from a source file or link, containing agent-friendly Markdown, source metadata, and reusable media references.
_Avoid_: Final document, rendered document

**Partial Parse Result**:
A parse result that contains usable artifacts while also reporting warnings for source regions or modalities that could not be parsed completely.
_Avoid_: Failed result, incomplete output

**Result Package**:
A parse result represented as a directory of related artifacts, including Markdown, media files, and a manifest that indexes them.
_Avoid_: Single JSON response, output folder

**Manifest**:
The structured index inside a result package that describes the source, parse outputs, media references, and metadata needed by downstream agents.
_Avoid_: Metadata blob, response JSON

**Manifest Revision**:
A versioned snapshot of a manifest after the initial parse or later derived asset changes.
_Avoid_: Manifest backup, history file

**Schema Version**:
The version identifier for the manifest contract that downstream agents use to decide whether they can safely consume a result package.
_Avoid_: API version, format version

**Source**:
The original offline file submitted for parsing, normally provided through file upload and optionally through a service-local path in deployment-local workflows.
_Avoid_: Input, asset

**Source Semantic Fidelity**:
The invariant that a Parse Result may expose source content in a more consumable form but must not change its semantic scope. Provider-side crops, zooms, rotations, and other inspection views never replace the complete source page or image in public output.
_Avoid_: Pixel-perfect reproduction, visual cleanup

**File Upload**:
The general source entry point where an agent sends file bytes to File2Doc over HTTP.
_Avoid_: Remote path, local path upload

**Service Local Path**:
An optional source entry point where the submitted path is resolved in the File2Doc service environment, not in the caller's machine.
_Avoid_: Local path

**Source File**:
The service-side file object created from a file upload or accepted service-local path before parsing begins.
_Avoid_: Uploaded blob, local input

**Content Identity**:
A hash-based identity for a source file's bytes, used to detect repeated submissions without replacing job or result identifiers.
_Avoid_: File ID, job ID

**Cache Retention**:
The period for which source files and result packages remain available before the service may delete them to control storage use.
_Avoid_: Expiration, cleanup

**Parser Profile**:
A named parsing configuration that determines how much structure, media, transcript, and visual detail a parse result should include.
_Avoid_: Preset, mode

**Page Image**:
A full-resolution visual rendering of a source page, kept for faithful review and downstream document layout.
_Avoid_: Screenshot

**Thumbnail**:
A low-resolution page or frame preview that helps agents quickly understand visual structure without loading full page images.
_Avoid_: Preview image, small screenshot

**OCR Layer**:
A legacy page-level compatibility artifact that exposes visible text and layout
from Visual Parsing to older manifest consumers. It is not produced by a
separate OCR engine; new consumers should use the linked Visual Parse Result.
_Avoid_: OCR engine output, OCR fallback

**Visual Parsing**:
Pixel-level interpretation performed by the configured vision-language model only for content that native parsing cannot recover, such as an embedded visual region or scanned page. It turns that content into semantic, agent-readable text while native PDF text remains owned by the native parser. File2Doc does not maintain a separate traditional OCR engine.
_Avoid_: Traditional OCR, image-to-text fallback, VLM OCR

**Visual Item**:
A scanned page render or embedded visual region selected from a PDF or Office source for Visual Parsing. It remains linked to its complete source page, slide, sheet, or document position and is not itself published as a replacement for that source unit.
_Avoid_: OCR input, screenshot, loose image

**Visual Parse Result**:
The semantic, agent-readable output produced for one Visual Item. It contains a generic description, visible text in source order, layout information, and uncertainty warnings. Domain-specific fields such as numeric candidates are not part of this generic result.
_Avoid_: OCR text, image caption, business extraction result

**Vision-Language Model**:
The configured remote model that owns all Visual Parsing in File2Doc, including visible-text recognition, image description, layout interpretation, and uncertainty reporting.
_Avoid_: OCR engine, image parser

**Visual Provider**:
The runtime adapter through which File2Doc invokes a Vision-Language Model. The first production Visual Provider uses the Volcengine Ark Responses API while the File2Doc parsing boundary remains provider-independent.
_Avoid_: OCR endpoint, model URL

**Visual Tool Action**:
A provider-side operation such as zooming or rotating a Visual Item to resolve small, ambiguous, or incorrectly oriented content. It is a temporary inspection view only. Safe action metadata may remain traceable, but the transformed image is not part of the Result Package.
_Avoid_: Image edit, preprocessing step

**Content Markdown**:
The clean, agent-readable Markdown body of a parse result. For PDF sources it retains complete page-image references and combines native text with semantic descriptions of unresolved visual regions.
_Avoid_: OCR dump, raw markdown

**Page Index**:
The page-level index in a manifest that links each source page to its thumbnail, page image, OCR layer, and parsing status.
_Avoid_: Image list, pages metadata

**Table Index**:
The table-level index in a manifest that summarizes sheets or tabular regions and points to full table artifacts when needed.
_Avoid_: Spreadsheet dump, table markdown

**Media Index**:
The unified catalog of visual assets in a result package, including page images, thumbnails, embedded images, and video frames.
_Avoid_: Image index, asset list

**Media Reference**:
A stable identifier used by content markdown and manifest views to refer to a media item without depending on its file path.
_Avoid_: Image path, file link

**Derived Asset**:
A media asset generated from an existing result package after the initial parse, such as a higher-DPI page image for one page.
_Avoid_: Regenerated output, extra image

**Parse Job**:
An asynchronous parsing task that tracks the lifecycle from source intake to result package availability.
_Avoid_: Request, task

**Work Item**:
An independently executable unit of work within a Parse Job, such as transcript generation, frame extraction, visual parsing, or result assembly. Work Items may run concurrently and fail or retry independently without creating additional user-visible Parse Jobs.
_Avoid_: Child job, sub-job, background task

**Execution Lease**:
A time-bounded claim that lets one worker execute a Work Item while heartbeats keep the claim active. An expired lease makes unfinished work eligible for another attempt, so a lost worker cannot leave a Parse Job permanently running.
_Avoid_: Lock, worker ownership, task timeout

**Progress Event**:
A human- and agent-readable update that reports the current stage, completion estimate, and relevant details of a parse job.
_Avoid_: Log line, status message

**Stage**:
A stable, high-level phase in a parse job lifecycle that lets agents and humans understand where parsing currently is.
_Avoid_: Parser step, internal state

**Warning**:
A non-fatal parsing problem, including a Visual Item that could not be interpreted, that downstream agents should consider when using a partial parse result. A warning does not prevent File2Doc from packaging other usable or explicitly empty outputs.
_Avoid_: Soft error, log warning

**Resource Limit**:
A configured bound on source size, duration, page count, concurrency, or output size that keeps parsing predictable and protects service capacity.
_Avoid_: Quota, guardrail

**Frame Extraction**:
The process of selecting still images from a video source so agents can understand visual changes without reading the full video.
_Avoid_: Video screenshotting, video truth-question logic

**Change-Aware Frame Extraction**:
Frame extraction that prefers visually meaningful changes over fixed time intervals, while keeping interval-based frames as fallback anchors.
_Avoid_: Fixed interval extraction

**Transcript**:
The text representation of speech in an audio or video source, usually aligned to source time when timestamps are available.
_Avoid_: ASR output, transcription blob

**Time-Aligned Transcript**:
A transcript whose segments carry source time ranges so agents can connect spoken content with nearby video frames.
_Avoid_: Timestamped ASR output

**Timeline Index**:
The time-based index that connects transcript segments, video frames, and media references for audio or video sources.
_Avoid_: Timeline, AV index
