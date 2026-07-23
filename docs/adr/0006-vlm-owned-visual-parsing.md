# VLM-owned visual parsing for document images

File2Doc uses a configured Vision-Language Model for all pixel-level
interpretation and does not maintain a separate traditional OCR engine.
MarkItDown remains responsible for native document structure and extractable
PDF text. Its enhanced converters invoke the Visual Provider only for embedded
visual regions or scanned pages that native parsing cannot interpret, then
insert a generic Visual Parse Result at the source position.

The first production provider is a File2Doc-owned adapter for the Volcengine
Ark Responses API, using structured output and Zoom/Rotate tool actions. Its
implementation is imported from the independently evolved
`file2doc-markitdown-visual` v0.3.1 code used by rd-assistant, with upstream
license and commit provenance retained, but without any repository, package,
secret, image, deployment, or release coupling to rd-assistant.

Visual Parsing runs automatically for embedded images in PDF, DOCX, PPTX, and
XLSX sources and for scanned PDF page renders. Native-text-only PDF pages do
not invoke the Visual Provider. The first release does not add
standalone image sources or parse video frames. Provider calls may send one
Visual Item to File2Doc's dedicated remote Ark endpoint; they never send the
whole Office or PDF source as one request. Individual failures and empty model
outputs produce warnings and partial or explicitly empty results rather than
failing an otherwise packageable Parse Job.

Each result contains description, visible text, layout, and uncertainty
warnings. File2Doc preserves source semantic fidelity: PDF output publishes the
complete page image and combines native text with visual-region semantics.
Decoded PDF visual objects and provider-side Zoom/Rotate views are inspection
inputs only; they do not enter Content Markdown, the Media Index, or the Result Package.
Office embedded images remain complete source media. Exact duplicate bytes
share one provider call only within the same Parse Job; cross-job caching is
deliberately deferred.
