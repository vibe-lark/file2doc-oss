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

from file2doc_markitdown_visual import __version__ as visual_plugin_version


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


@dataclass(frozen=True)
class ParseOptions:
    markitdown_factory: MarkItDownFactory = lambda: MarkItDown(enable_plugins=False)
    ocr_runner: OcrRunner | None = None
    ocr_model: str | None = None
    ocr_api_key: str | None = None
    ocr_base_url: str | None = None
    ocr_timeout_seconds: float = 300
    ocr_page_render_scale: float = 1.5
    visual_client: Any | None = None
    visual_model: str | None = None
    visual_api_key: str | None = None
    visual_base_url: str | None = "https://ark.cn-beijing.volces.com/api/v3"
    visual_timeout_seconds: float = 300

    @classmethod
    def from_env(cls) -> "ParseOptions":
        return cls(
            ocr_model=os.getenv("FILE2DOC_OCR_MODEL"),
            ocr_api_key=os.getenv("FILE2DOC_OCR_API_KEY")
            or os.getenv("OPENAI_API_KEY"),
            ocr_base_url=os.getenv("FILE2DOC_OCR_BASE_URL"),
            ocr_timeout_seconds=_env_float("FILE2DOC_OCR_TIMEOUT_SECONDS", 300),
            ocr_page_render_scale=_env_float("FILE2DOC_OCR_PAGE_RENDER_SCALE", 1.5),
            visual_model=os.getenv("FILE2DOC_VISUAL_MODEL"),
            visual_api_key=os.getenv("FILE2DOC_VISUAL_API_KEY"),
            visual_base_url=os.getenv("FILE2DOC_VISUAL_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/v3",
            visual_timeout_seconds=_env_float("FILE2DOC_VISUAL_TIMEOUT_SECONDS", 300),
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
    def visual_configured(self) -> bool:
        return self.visual_client is not None or bool(
            self.visual_model and self.visual_api_key
        )

    def get_visual_client(self) -> Any:
        if self.visual_client is not None:
            return self.visual_client
        if not self.visual_model or not self.visual_api_key:
            raise ParseFailure(
                "visual_parsing_not_configured",
                "Image parsing requires FILE2DOC_VISUAL_MODEL and "
                "FILE2DOC_VISUAL_API_KEY",
            )
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - deployment dependency.
            raise ParseFailure(
                "visual_parsing_not_configured",
                "Visual parsing requires the openai package",
            ) from error
        client_kwargs: dict[str, Any] = {
            "api_key": self.visual_api_key,
            "base_url": self.visual_base_url
            or "https://ark.cn-beijing.volces.com/api/v3",
            "timeout": self.visual_timeout_seconds,
        }
        return OpenAI(**client_kwargs)


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
        return _parse_image_with_visual_plugin(
            source_path,
            content_type,
            parse_options,
            started_at,
        )

    try:
        result = parse_options.markitdown_factory().convert(source_path)
    except (
        Exception
    ) as error:  # pragma: no cover - exact converter errors vary by dependency.
        raise ParseFailure("parser_failed", f"MarkItDown failed: {error}") from error

    content = result.text_content.strip()
    if not content:
        if _is_pdf(content_type) and parse_options.ocr_configured:
            return _parse_pdf_with_ocr(source_path, parse_options, started_at)
        raise ParseFailure(
            "empty_parse_result", "MarkItDown produced no usable Markdown"
        )
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
        raise ParseFailure(
            "remote_ocr_failed", f"OCR recovery failed: {error}"
        ) from error

    if not content:
        raise ParseFailure(
            "remote_ocr_failed", "OCR recovery produced no usable Markdown"
        )

    return ParsedContent(
        markdown=content + "\n",
        diagnostics=_diagnostics(
            name=(
                "markitdown-ocr"
                if options.ocr_runner is not None
                else "direct-pdf-vision-ocr"
            ),
            version=_package_version("markitdown-ocr")
            or _package_version("markitdown"),
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=True,
            remote_services_used=True,
        ),
    )


def _parse_image_with_visual_plugin(
    source_path: Path,
    content_type: str,
    options: ParseOptions,
    started_at: float,
) -> ParsedContent:
    if not options.visual_configured:
        raise ParseFailure(
            "visual_parsing_not_configured",
            "Image parsing requires FILE2DOC_VISUAL_MODEL and FILE2DOC_VISUAL_API_KEY",
        )

    try:
        result = MarkItDown(
            enable_builtins=False,
            enable_plugins=True,
            visual_client=options.get_visual_client(),
            visual_model=options.visual_model,
        ).convert(source_path)
        content = result.text_content.strip()
    except Exception as error:  # MarkItDown wraps converter failures by design.
        raise ParseFailure(
            "visual_item_failed",
            f"Visual parsing failed for {source_path.name}: {error}",
        ) from error

    if not content:
        raise ParseFailure(
            "visual_item_failed",
            f"Visual parsing produced no usable Markdown for {source_path.name}",
        )

    markdown = content.rstrip()
    return ParsedContent(
        markdown=markdown + "\n",
        diagnostics=_diagnostics(
            name="file2doc-markitdown-visual",
            version=visual_plugin_version,
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=True,
            remote_services_used=True,
            source_media_type=content_type.split(";", 1)[0].strip().lower(),
            markitdown_version=_package_version("markitdown"),
            visual_plugin_version=visual_plugin_version,
            visual_model=options.visual_model,
            visual_provider="ark-responses",
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
        raise ParseFailure(
            "remote_ocr_failed", f"MarkItDown OCR failed: {error}"
        ) from error

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
        result_queue.put(
            {"ok": True, "text": _run_markitdown_ocr(source_path, options)}
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


def _run_direct_pdf_vision_ocr_with_timeout(
    source_path: Path,
    options: ParseOptions,
) -> str:
    return _run_ocr_process_with_timeout(
        source_path,
        options,
        _run_direct_pdf_vision_ocr_worker,
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


def _env_float(name: str, default: float) -> float:
    configured = os.getenv(name)
    if configured is None:
        return default
    try:
        value = float(configured)
    except ValueError:
        return default
    return value if value > 0 else default
