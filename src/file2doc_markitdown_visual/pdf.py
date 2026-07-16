from __future__ import annotations

import hashlib
import io
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from time import monotonic
from typing import Any, BinaryIO

import pdfplumber
import pypdfium2 as pdfium
from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo

from .plugin import (
    VisualExecutionConfig,
    VisualExecutionPolicy,
    VisualArtifactCollector,
    DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
    VisualImageConverter,
    VisualParseError,
)


@dataclass(frozen=True)
class _PageItem:
    y: float
    x: float
    markdown: str | None = None
    image: bytes | None = None
    location: str | None = None
    failure: str | None = None


class VisualPdfConverter(DocumentConverter):
    """Convert PDF pixel content through the same visual image boundary."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        execution_policy: VisualExecutionPolicy | None = None,
        execution_config: VisualExecutionConfig | None = None,
        item_timeout_seconds: float = 300,
        job_deadline_seconds: float = 900,
        max_concurrency: int = 4,
        artifact_collector: VisualArtifactCollector | None = None,
        artifact_allowed_hosts: tuple[str, ...] = DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
    ) -> None:
        self._execution_policy = execution_policy
        self._execution_config = execution_config or VisualExecutionConfig(
            item_timeout_seconds=item_timeout_seconds,
            job_deadline_seconds=job_deadline_seconds,
            max_concurrency=max_concurrency,
        )
        self._client = client
        self._model = model
        self._artifact_collector = artifact_collector
        self._artifact_allowed_hosts = artifact_allowed_hosts

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        return extension == ".pdf" or mimetype.startswith("application/pdf")

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        file_stream.seek(0)
        source_bytes = file_stream.read()
        execution_policy = self._execution_policy or self._execution_config.create_policy()
        image_converter = VisualImageConverter(
            client=self._client,
            model=self._model,
            execution_policy=execution_policy,
            artifact_collector=self._artifact_collector,
            artifact_allowed_hosts=self._artifact_allowed_hosts,
        )
        item_timeout_seconds = execution_policy.item_timeout_seconds
        job_deadline_seconds = execution_policy.job_deadline_seconds
        max_concurrency = execution_policy.max_concurrency
        deadline_at = execution_policy.deadline_at
        page_items: list[list[_PageItem]] = []
        extraction_warnings: list[str] = []
        try:
            with pdfplumber.open(io.BytesIO(source_bytes)) as document:
                for page_index, page in enumerate(document.pages, 1):
                    items = _page_items(page, page_index=page_index)
                    if not any(item.markdown for item in items) and not any(
                        item.image for item in items
                    ):
                        items.append(
                            _PageItem(
                                y=0,
                                x=0,
                                image=_render_full_page(source_bytes, page_index),
                                location=f"page {page_index}",
                            )
                        )
                    page_items.append(
                        sorted(items, key=lambda value: (value.y, value.x))
                    )
        except Exception as error:
            page_items = _recover_page_items(source_bytes)
            extraction_warnings.append(
                "PDF structure required page-render recovery: " + str(error)
            )

        visual_results = self._parse_visual_items(
            [item for items in page_items for item in items if item.image is not None],
            kwargs,
            image_converter=image_converter,
            deadline_at=deadline_at,
            item_timeout_seconds=item_timeout_seconds,
            job_deadline_seconds=job_deadline_seconds,
            max_concurrency=max_concurrency,
        )
        pages: list[str] = []
        warnings = list(extraction_warnings)
        usable_items = 0
        for page_index, items in enumerate(page_items, 1):
            page_parts: list[str] = []
            for item in items:
                if item.markdown:
                    page_parts.append(item.markdown)
                    usable_items += 1
                    continue
                if item.failure:
                    warnings.append(f"{item.location}: {item.failure}")
                    page_parts.append(
                        f"> [!WARNING] Visual parsing skipped for "
                        f"{item.location}: {item.failure}"
                    )
                    continue
                assert item.image is not None
                content_hash = hashlib.sha256(item.image).hexdigest()
                succeeded, visual_markdown = visual_results[content_hash]
                if not succeeded:
                    warnings.append(f"{item.location}: {visual_markdown}")
                    page_parts.append(
                        f"> [!WARNING] Visual parsing skipped for "
                        f"{item.location}: {visual_markdown}"
                    )
                    continue
                usable_items += 1
                page_parts.append(
                    f"### Visual item: {item.location}\n\n{visual_markdown}"
                )
            pages.append(f"## Page {page_index}\n\n" + "\n\n".join(page_parts))

        if usable_items == 0:
            detail = "; ".join(warnings) or "no native text or visual content"
            raise VisualParseError(f"PDF produced no usable content: {detail}")

        warning_section = ""
        if warnings:
            warning_section = "## Warnings\n\n" + "\n".join(
                f"- {warning}" for warning in warnings
            ) + "\n\n"
        return DocumentConverterResult(
            markdown=(
                "# PDF Visual Parse Result\n\n"
                + warning_section
                + "\n\n".join(pages)
            )
        )

    def _parse_visual_items(
        self,
        items: list[_PageItem],
        kwargs: dict[str, Any],
        *,
        image_converter: VisualImageConverter,
        deadline_at: float,
        item_timeout_seconds: float,
        job_deadline_seconds: float,
        max_concurrency: int,
    ) -> dict[str, tuple[bool, str]]:
        unique: dict[str, _PageItem] = {}
        for item in items:
            assert item.image is not None
            unique.setdefault(hashlib.sha256(item.image).hexdigest(), item)

        queued = list(unique.items())
        results: dict[str, tuple[bool, str]] = {}
        pending: dict[Future, tuple[str, float]] = {}
        executor = ThreadPoolExecutor(max_workers=max_concurrency)

        def submit_available() -> None:
            while (
                queued
                and len(pending) < max_concurrency
                and monotonic() < deadline_at
            ):
                content_hash, item = queued.pop(0)
                request_timeout = min(
                    item_timeout_seconds,
                    max(deadline_at - monotonic(), 0.001),
                )
                future = executor.submit(
                    self._parse_visual_item,
                    image_converter,
                    item,
                    kwargs,
                    request_timeout,
                )
                pending[future] = (content_hash, monotonic())

        try:
            submit_available()
            while pending or queued:
                now = monotonic()
                if now >= deadline_at:
                    break
                submit_available()
                if not pending:
                    break
                next_item_timeout = min(
                    started_at + item_timeout_seconds - now
                    for _, started_at in pending.values()
                )
                completed, _ = wait(
                    pending,
                    timeout=max(
                        0,
                        min(deadline_at - now, next_item_timeout),
                    ),
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    content_hash, _ = pending.pop(future)
                    try:
                        results[content_hash] = (True, future.result())
                    except Exception as error:
                        results[content_hash] = (False, str(error))

                now = monotonic()
                timed_out = [
                    future
                    for future, (_, started_at) in pending.items()
                    if now - started_at >= item_timeout_seconds
                ]
                for future in timed_out:
                    content_hash, _ = pending.pop(future)
                    future.cancel()
                    results[content_hash] = (
                        False,
                        "visual item timed out after "
                        f"{item_timeout_seconds:g} seconds",
                    )
                submit_available()
        finally:
            if pending or queued:
                reason = (
                    "visual job deadline exceeded after "
                    f"{job_deadline_seconds:g} seconds"
                )
                for future, (content_hash, _) in pending.items():
                    future.cancel()
                    results[content_hash] = (False, reason)
                for content_hash, _ in queued:
                    results[content_hash] = (False, reason)
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    def _parse_visual_item(
        self,
        image_converter: VisualImageConverter,
        item: _PageItem,
        kwargs: dict[str, Any],
        request_timeout: float,
    ) -> str:
        assert item.image is not None
        visual = image_converter.convert(
            io.BytesIO(item.image),
            StreamInfo(filename="pdf-visual.png", mimetype="image/png"),
            **kwargs,
        )
        return visual.markdown


def _page_items(page: Any, *, page_index: int) -> list[_PageItem]:
    words = sorted(page.extract_words(), key=lambda word: (word["top"], word["x0"]))
    text_lines: list[list[dict]] = []
    for word in words:
        if not str(word.get("text", "")).strip():
            continue
        if not text_lines or abs(float(word["top"]) - float(text_lines[-1][0]["top"])) > 2:
            text_lines.append([word])
        else:
            text_lines[-1].append(word)
    items = [
        _PageItem(
            y=float(line[0]["top"]),
            x=min(float(word["x0"]) for word in line),
            markdown=" ".join(str(word["text"]) for word in line),
        )
        for line in text_lines
    ]
    for image_index, image in enumerate(page.images, 1):
        x0 = float(image.get("x0", 0))
        top = float(image.get("top", 0))
        x1 = float(image.get("x1", x0))
        bottom = float(image.get("bottom", top))
        location = f"page {page_index} image {image_index}"
        if x1 <= x0 or bottom <= top:
            items.append(
                _PageItem(
                    y=top,
                    x=x0,
                    location=location,
                    failure="image region has invalid bounds",
                )
            )
            continue
        try:
            cropped = page.crop((x0, top, x1, bottom)).to_image(resolution=144)
            buffer = io.BytesIO()
            cropped.original.save(buffer, format="PNG")
            items.append(
                _PageItem(
                    y=top,
                    x=x0,
                    image=buffer.getvalue(),
                    location=location,
                )
            )
        except Exception as error:
            items.append(
                _PageItem(
                    y=top,
                    x=x0,
                    location=location,
                    failure=f"could not render image region: {error}",
                )
            )
    return items


def _render_full_page(source_bytes: bytes, page_index: int) -> bytes:
    document = pdfium.PdfDocument(source_bytes)
    try:
        image = document[page_index - 1].render(scale=2).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        document.close()


def _recover_page_items(source_bytes: bytes) -> list[list[_PageItem]]:
    try:
        document = pdfium.PdfDocument(source_bytes)
    except Exception as error:
        raise VisualParseError(f"PDF could not be opened for recovery: {error}") from error
    pages: list[list[_PageItem]] = []
    try:
        for page_index in range(1, len(document) + 1):
            page = document[page_index - 1]
            text_page = page.get_textpage()
            try:
                native_text = text_page.get_text_range().strip()
            finally:
                text_page.close()
            image = page.render(scale=2).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            items: list[_PageItem] = []
            if native_text:
                items.append(_PageItem(y=0, x=0, markdown=native_text))
            items.append(
                _PageItem(
                    y=1,
                    x=0,
                    image=buffer.getvalue(),
                    location=f"page {page_index} recovery render",
                )
            )
            pages.append(items)
    finally:
        document.close()
    return pages
