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
A named parsing configuration that determines how much structure, media, OCR, transcript, and visual detail a parse result should include.
_Avoid_: Preset, mode

**Page Image**:
A full-resolution visual rendering of a source page, kept for faithful review and downstream document layout.
_Avoid_: Screenshot

**Thumbnail**:
A low-resolution page or frame preview that helps agents quickly understand visual structure without loading full page images.
_Avoid_: Preview image, small screenshot

**OCR Layer**:
Text and layout information attached to a page image, used when agents need source-page text without inspecting the full image.
_Avoid_: OCR result, extracted text

**OCR Fallback**:
An OCR layer produced by a separate OCR engine when the primary parser cannot provide reliable page text.
_Avoid_: Extra OCR, backup text extraction

**Content Markdown**:
The clean, agent-readable Markdown body of a parse result, optimized for understanding and rearrangement rather than source-page fidelity.
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

**Progress Event**:
A human- and agent-readable update that reports the current stage, completion estimate, and relevant details of a parse job.
_Avoid_: Log line, status message

**Stage**:
A stable, high-level phase in a parse job lifecycle that lets agents and humans understand where parsing currently is.
_Avoid_: Parser step, internal state

**Warning**:
A non-fatal parsing problem that downstream agents should consider when using a partial parse result.
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
