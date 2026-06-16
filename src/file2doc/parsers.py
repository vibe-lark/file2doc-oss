from __future__ import annotations

from dataclasses import dataclass, field
import base64
import io
from importlib.metadata import PackageNotFoundError, version
import multiprocessing
import os
from pathlib import Path
import queue
from time import perf_counter
from typing import Any, Callable

from markitdown import MarkItDown


@dataclass(frozen=True)
class ParsedContent:
    markdown: str
    diagnostics: dict
    warnings: list[dict] = field(default_factory=list)


class ParseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


MarkItDownFactory = Callable[[], Any]
OcrRunner = Callable[[Path, "ParseOptions"], str]
ImageRunner = Callable[[Path, "ParseOptions"], str]


@dataclass(frozen=True)
class ParseOptions:
    markitdown_factory: MarkItDownFactory = lambda: MarkItDown(enable_plugins=False)
    ocr_runner: OcrRunner | None = None
    image_runner: ImageRunner | None = None
    ocr_model: str | None = None
    ocr_api_key: str | None = None
    ocr_base_url: str | None = None
    ocr_timeout_seconds: float = 300
    ocr_page_render_scale: float = 1.5

    @classmethod
    def from_env(cls) -> "ParseOptions":
        return cls(
            ocr_model=os.getenv("FILE2DOC_OCR_MODEL"),
            ocr_api_key=os.getenv("FILE2DOC_OCR_API_KEY") or os.getenv("OPENAI_API_KEY"),
            ocr_base_url=os.getenv("FILE2DOC_OCR_BASE_URL"),
            ocr_timeout_seconds=_env_float("FILE2DOC_OCR_TIMEOUT_SECONDS", 300),
            ocr_page_render_scale=_env_float("FILE2DOC_OCR_PAGE_RENDER_SCALE", 1.5),
        )

    @property
    def ocr_configured(self) -> bool:
        return self.ocr_runner is not None or bool(self.ocr_model and self.ocr_api_key)

    def run_ocr(self, source_path: Path) -> str:
        if self.ocr_runner is not None:
            return self.ocr_runner(source_path, self)

        if not self.ocr_model or not self.ocr_api_key:
            raise ParseFailure(
                "empty_parse_result",
                "MarkItDown produced no usable Markdown and OCR is not configured",
            )

        return _run_direct_pdf_vision_ocr_with_timeout(source_path, self)

    @property
    def image_vision_configured(self) -> bool:
        return self.image_runner is not None or bool(self.ocr_model and self.ocr_api_key)

    def run_image_vision(self, source_path: Path) -> str:
        if self.image_runner is not None:
            return self.image_runner(source_path, self)

        if not self.ocr_model or not self.ocr_api_key:
            raise ParseFailure(
                "image_vision_not_configured",
                "Image parsing requires a configured OpenAI-compatible vision endpoint",
            )

        return _run_direct_image_vision_with_timeout(source_path, self)


def parse_content_markdown(
    source_path: Path,
    content_type: str,
    options: ParseOptions | None = None,
) -> ParsedContent:
    started_at = perf_counter()
    parse_options = options or ParseOptions.from_env()

    if content_type.startswith("text/"):
        content = source_path.read_text(encoding="utf-8")
        return ParsedContent(
            markdown=content,
            diagnostics=_diagnostics(
                name="plain-text",
                version=None,
                elapsed_ms=_elapsed_ms(started_at),
            ),
        )

    if _is_image(content_type):
        return _parse_image_with_vision(source_path, content_type, parse_options, started_at)

    try:
        result = parse_options.markitdown_factory().convert(source_path)
    except Exception as error:  # pragma: no cover - exact converter errors vary by dependency.
        raise ParseFailure("parser_failed", f"MarkItDown failed: {error}") from error

    content = result.text_content.strip()
    if not content:
        if _is_pdf(content_type) and parse_options.ocr_configured:
            return _parse_pdf_with_ocr(source_path, parse_options, started_at)
        raise ParseFailure("empty_parse_result", "MarkItDown produced no usable Markdown")
    return ParsedContent(
        markdown=content + "\n",
        diagnostics=_diagnostics(
            name="markitdown",
            version=_package_version("markitdown"),
            elapsed_ms=_elapsed_ms(started_at),
        ),
    )


def _parse_pdf_with_ocr(
    source_path: Path,
    options: ParseOptions,
    started_at: float,
) -> ParsedContent:
    try:
        content = options.run_ocr(source_path).strip()
    except ParseFailure:
        raise
    except Exception as error:  # pragma: no cover - exact remote client errors vary.
        raise ParseFailure("remote_ocr_failed", f"OCR recovery failed: {error}") from error

    if not content:
        raise ParseFailure("remote_ocr_failed", "OCR recovery produced no usable Markdown")

    return ParsedContent(
        markdown=content + "\n",
        diagnostics=_diagnostics(
            name=(
                "markitdown-ocr"
                if options.ocr_runner is not None
                else "direct-pdf-vision-ocr"
            ),
            version=_package_version("markitdown-ocr") or _package_version("markitdown"),
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=True,
            remote_services_used=True,
        ),
    )


def _parse_image_with_vision(
    source_path: Path,
    content_type: str,
    options: ParseOptions,
    started_at: float,
) -> ParsedContent:
    if not options.image_vision_configured:
        raise ParseFailure(
            "image_vision_not_configured",
            "Image parsing requires a configured OpenAI-compatible vision endpoint",
        )

    try:
        content = options.run_image_vision(source_path).strip()
    except ParseFailure:
        raise
    except Exception as error:  # pragma: no cover - exact remote client errors vary.
        raise ParseFailure("image_vision_failed", f"Image vision parsing failed: {error}") from error

    if not content:
        raise ParseFailure("empty_parse_result", "Image vision parsing produced no usable Markdown")

    markdown = _normalize_image_markdown(content)
    return ParsedContent(
        markdown=markdown + "\n",
        diagnostics=_diagnostics(
            name="image-vision",
            version=_package_version("openai"),
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=True,
            remote_services_used=True,
            source_media_type=content_type.split(";", 1)[0].strip().lower(),
        ),
        warnings=_warnings_from_markdown(markdown),
    )


def _run_markitdown_ocr(source_path: Path, options: ParseOptions) -> str:
    try:
        from openai import OpenAI
    except ImportError as error:  # pragma: no cover - depends on deployment extras.
        raise ParseFailure(
            "remote_ocr_failed",
            "OCR recovery is configured but the openai package is not installed",
        ) from error

    client_kwargs = {
        "api_key": options.ocr_api_key,
        "timeout": options.ocr_timeout_seconds,
    }
    if options.ocr_base_url:
        client_kwargs["base_url"] = options.ocr_base_url

    client = OpenAI(**client_kwargs)
    try:
        result = MarkItDown(
            enable_plugins=True,
            llm_client=client,
            llm_model=options.ocr_model,
        ).convert(source_path)
    except Exception as error:  # pragma: no cover - exact converter/client errors vary.
        raise ParseFailure("remote_ocr_failed", f"MarkItDown OCR failed: {error}") from error

    return result.text_content


def _run_markitdown_ocr_with_timeout(source_path: Path, options: ParseOptions) -> str:
    timeout = options.ocr_timeout_seconds
    result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=_run_markitdown_ocr_worker,
        args=(source_path, options, result_queue),
    )
    process.start()
    process.join(timeout)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise ParseFailure(
            "remote_ocr_failed",
            f"OCR recovery timed out after {timeout:g} seconds",
        )

    try:
        result = result_queue.get_nowait()
    except queue.Empty as error:
        raise ParseFailure(
            "remote_ocr_failed",
            f"OCR recovery process exited with code {process.exitcode}",
        ) from error

    if result["ok"]:
        return result["text"]

    raise ParseFailure(result["code"], result["message"])


def _run_markitdown_ocr_worker(
    source_path: Path,
    options: ParseOptions,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        result_queue.put({"ok": True, "text": _run_markitdown_ocr(source_path, options)})
    except ParseFailure as error:
        result_queue.put({"ok": False, "code": error.code, "message": str(error)})
    except Exception as error:  # pragma: no cover - exact dependency failures vary.
        result_queue.put(
            {
                "ok": False,
                "code": "remote_ocr_failed",
                "message": f"OCR recovery failed: {error}",
            }
        )


def _run_direct_pdf_vision_ocr_with_timeout(
    source_path: Path,
    options: ParseOptions,
) -> str:
    return _run_ocr_process_with_timeout(
        source_path,
        options,
        _run_direct_pdf_vision_ocr_worker,
    )


def _run_direct_image_vision_with_timeout(
    source_path: Path,
    options: ParseOptions,
) -> str:
    return _run_ocr_process_with_timeout(
        source_path,
        options,
        _run_direct_image_vision_worker,
        failure_code="image_vision_failed",
        failure_label="Image vision parsing",
    )


def _run_ocr_process_with_timeout(
    source_path: Path,
    options: ParseOptions,
    target: Callable[[Path, ParseOptions, multiprocessing.Queue], None],
    *,
    failure_code: str = "remote_ocr_failed",
    failure_label: str = "OCR recovery",
) -> str:
    timeout = options.ocr_timeout_seconds
    result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(
        target=target,
        args=(source_path, options, result_queue),
    )
    process.start()
    process.join(timeout)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise ParseFailure(
            failure_code,
            f"{failure_label} timed out after {timeout:g} seconds",
        )

    try:
        result = result_queue.get_nowait()
    except queue.Empty as error:
        raise ParseFailure(
            failure_code,
            f"{failure_label} process exited with code {process.exitcode}",
        ) from error

    if result["ok"]:
        return result["text"]

    raise ParseFailure(result["code"], result["message"])


def _run_direct_pdf_vision_ocr_worker(
    source_path: Path,
    options: ParseOptions,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        result_queue.put(
            {"ok": True, "text": _run_direct_pdf_vision_ocr(source_path, options)}
        )
    except ParseFailure as error:
        result_queue.put({"ok": False, "code": error.code, "message": str(error)})
    except Exception as error:  # pragma: no cover - exact dependency failures vary.
        result_queue.put(
            {
                "ok": False,
                "code": "remote_ocr_failed",
                "message": f"OCR recovery failed: {error}",
            }
        )


def _run_direct_image_vision_worker(
    source_path: Path,
    options: ParseOptions,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        result_queue.put(
            {"ok": True, "text": _run_direct_image_vision(source_path, options)}
        )
    except ParseFailure as error:
        result_queue.put({"ok": False, "code": error.code, "message": str(error)})
    except Exception as error:  # pragma: no cover - exact dependency failures vary.
        result_queue.put(
            {
                "ok": False,
                "code": "image_vision_failed",
                "message": f"Image vision parsing failed: {error}",
            }
        )


def _run_direct_pdf_vision_ocr(source_path: Path, options: ParseOptions) -> str:
    try:
        import pypdfium2 as pdfium
        from openai import OpenAI
    except ImportError as error:  # pragma: no cover - depends on deployment extras.
        raise ParseFailure(
            "remote_ocr_failed",
            "Direct PDF OCR requires pypdfium2 and openai packages",
        ) from error

    if not options.ocr_model or not options.ocr_api_key:
        raise ParseFailure(
            "empty_parse_result",
            "MarkItDown produced no usable Markdown and OCR is not configured",
        )

    client_kwargs = {
        "api_key": options.ocr_api_key,
        "timeout": options.ocr_timeout_seconds,
    }
    if options.ocr_base_url:
        client_kwargs["base_url"] = options.ocr_base_url
    client = OpenAI(**client_kwargs)

    document = pdfium.PdfDocument(source_path)
    page_texts: list[str] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            image = page.render(scale=options.ocr_page_render_scale).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
            page_text = _extract_page_text_with_vision(
                client,
                model=options.ocr_model,
                encoded_png=encoded,
            )
            if page_text.strip():
                page_texts.append(f"## Page {page_index + 1}\n\n{page_text.strip()}")
    finally:
        document.close()

    return "\n\n".join(page_texts).strip()


def _run_direct_image_vision(source_path: Path, options: ParseOptions) -> str:
    try:
        from openai import OpenAI
    except ImportError as error:  # pragma: no cover - depends on deployment extras.
        raise ParseFailure(
            "image_vision_failed",
            "Image parsing is configured but the openai package is not installed",
        ) from error

    if not options.ocr_model or not options.ocr_api_key:
        raise ParseFailure(
            "image_vision_not_configured",
            "Image parsing requires a configured OpenAI-compatible vision endpoint",
        )

    client_kwargs = {
        "api_key": options.ocr_api_key,
        "timeout": options.ocr_timeout_seconds,
    }
    if options.ocr_base_url:
        client_kwargs["base_url"] = options.ocr_base_url
    client = OpenAI(**client_kwargs)

    media_type = _image_media_type_from_path(source_path)
    encoded = base64.b64encode(source_path.read_bytes()).decode("utf-8")
    response = client.chat.completions.create(
        model=options.ocr_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Parse this image into generic Markdown for downstream agents. "
                            "Return Markdown only with these sections: "
                            "Visible Text, Candidate Numeric Values, Layout, Warnings. "
                            "Do not return domain-specific JSON. Use 'None.' in Warnings "
                            "when no material warning is present."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    },
                ],
            }
        ],
        max_tokens=2000,
    )
    return response.choices[0].message.content or ""


def _extract_page_text_with_vision(client: Any, *, model: str, encoded_png: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract all visible text from this PDF page image. "
                            "Return only the extracted text, preserving reading order."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded_png}"},
                    },
                ],
            }
        ],
        max_tokens=2000,
    )
    return response.choices[0].message.content or ""


def _diagnostics(
    *,
    name: str,
    version: str | None,
    elapsed_ms: float,
    ocr_used: bool = False,
    remote_services_used: bool = False,
    **extra: Any,
) -> dict:
    diagnostics = {
        "name": name,
        "version": version,
        "elapsed_ms": elapsed_ms,
        "ocr_used": ocr_used,
        "remote_services_used": remote_services_used,
    }
    diagnostics.update(extra)
    return diagnostics


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def _is_pdf(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/pdf"


def _is_image(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in {
        "image/png",
        "image/jpeg",
        "image/jpg",
    }


def _image_media_type_from_path(source_path: Path) -> str:
    return "image/png" if source_path.suffix.lower() == ".png" else "image/jpeg"


def _warnings_from_markdown(markdown: str) -> list[dict]:
    lines = markdown.splitlines()
    warning_lines: list[str] = []
    in_warnings = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## warnings":
            in_warnings = True
            continue
        if in_warnings and stripped.startswith("## "):
            break
        if in_warnings and stripped:
            warning_lines.append(stripped.lstrip("-* ").strip())

    if not warning_lines:
        return []

    message = " ".join(warning_lines).strip()
    if message.lower().rstrip(".") in {"none", "no warnings", "none detected"}:
        return []

    return [{"code": "image_parse_warning", "message": message}]


def _normalize_image_markdown(markdown: str) -> str:
    required_headings = (
        "## Visible Text",
        "## Candidate Numeric Values",
        "## Layout",
        "## Warnings",
    )
    if all(heading.lower() in markdown.lower() for heading in required_headings):
        return markdown.rstrip()

    return "\n".join(
        [
            "# Image Analysis",
            "",
            "## Visible Text",
            "",
            markdown.strip(),
            "",
            "## Candidate Numeric Values",
            "",
            "None.",
            "",
            "## Layout",
            "",
            "Not described.",
            "",
            "## Warnings",
            "",
            "None.",
        ]
    )


def _env_float(name: str, default: float) -> float:
    configured = os.getenv(name)
    if configured is None:
        return default
    try:
        value = float(configured)
    except ValueError:
        return default
    return value if value > 0 else default
