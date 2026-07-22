# VLM-owned visual parsing for document images

File2Doc uses a configured Vision-Language Model for all pixel-level
interpretation and does not maintain a separate traditional OCR engine.
MarkItDown remains responsible for native document structure; its enhanced
converters extract each embedded image or scanned PDF page render, invoke the
Visual Provider independently, and insert a generic Visual Parse Result at the
source position so downstream agents do not need to inspect the image first.

The first production provider is a File2Doc-owned adapter for the Volcengine
Ark Responses API, using structured output and Zoom/Rotate tool actions. Its
implementation is imported from the independently evolved
`file2doc-markitdown-visual` v0.3.1 code used by rd-assistant, with upstream
license and commit provenance retained, but without any repository, package,
secret, image, deployment, or release coupling to rd-assistant.

Visual Parsing runs automatically for embedded images in PDF, DOCX, PPTX, and
XLSX sources and for scanned PDF page renders. The first release does not add
standalone image sources or parse video frames. Provider calls may send one
Visual Item to File2Doc's dedicated remote Ark endpoint; they never send the
whole Office or PDF source as one request. Individual failures and empty model
outputs produce warnings and partial or explicitly empty results rather than
failing an otherwise packageable Parse Job.

Each result contains description, visible text, layout, and uncertainty
warnings. File2Doc preserves the original media, stores structured JSON, and
inserts concise semantic Markdown at the original position. Exact duplicate
bytes share one provider call only within the same Parse Job; cross-job caching
is deliberately deferred.
