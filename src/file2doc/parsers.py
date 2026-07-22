from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from markitdown import MarkItDown

from file2doc_markitdown_visual import __version__ as visual_plugin_version
from file2doc_markitdown_visual import register_converters as register_visual_converters
from file2doc_markitdown_visual.plugin import (
    DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
    VISUAL_PROMPT_VERSION,
    VISUAL_PROVIDER,
    VISUAL_RESULT_SCHEMA_VERSION,
    VisualArtifactCollector,
    VisualDiagnosticArtifact,
    VisualParseRecord,
    VisualSourceRecord,
)


@dataclass(frozen=True)
class ParsedContent:
    markdown: str
    diagnostics: dict
    warnings: list[dict] = field(default_factory=list)
    visual_artifacts: tuple[VisualDiagnosticArtifact, ...] = ()
    visual_sources: tuple[VisualSourceRecord, ...] = ()
    visual_results: tuple[VisualParseRecord, ...] = ()


class ParseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


MarkItDownFactory = Callable[[], Any]


@dataclass(frozen=True)
class ParseOptions:
    markitdown_factory: MarkItDownFactory = lambda: MarkItDown(enable_plugins=False)
    visual_client: Any | None = None
    visual_model: str | None = None
    visual_api_key: str | None = None
    visual_base_url: str | None = "https://ark.cn-beijing.volces.com/api/v3"
    visual_item_timeout_seconds: float = 300
    visual_job_deadline_seconds: float = 900
    visual_max_concurrency: int = 4
    visual_artifact_allowed_hosts: tuple[str, ...] = (
        DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS
    )

    @classmethod
    def from_env(cls) -> "ParseOptions":
        return cls(
            visual_model=os.getenv("FILE2DOC_VISUAL_MODEL"),
            visual_api_key=os.getenv("FILE2DOC_VISUAL_API_KEY"),
            visual_base_url=os.getenv("FILE2DOC_VISUAL_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/v3",
            visual_item_timeout_seconds=_env_float(
                "FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS", 300
            ),
            visual_job_deadline_seconds=_env_float(
                "FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS", 900
            ),
            visual_max_concurrency=_env_int("FILE2DOC_VISUAL_MAX_CONCURRENCY", 4),
            visual_artifact_allowed_hosts=_env_hosts(
                "FILE2DOC_VISUAL_ARTIFACT_ALLOWED_HOSTS",
                DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
            ),
        )

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
                "Visual parsing requires FILE2DOC_VISUAL_MODEL and "
                "FILE2DOC_VISUAL_API_KEY",
            )
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - deployment dependency.
            raise ParseFailure(
                "visual_parsing_not_configured",
                "Visual parsing requires the openai package",
            ) from error
        return OpenAI(
            api_key=self.visual_api_key,
            base_url=self.visual_base_url,
            timeout=self.visual_item_timeout_seconds,
        )


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
                empty_result=not content.strip(),
            ),
        )

    if _is_visual_document(content_type) and parse_options.visual_configured:
        return _parse_with_visual_plugin(
            source_path,
            content_type,
            parse_options,
            started_at,
        )

    return _parse_standard(source_path, parse_options, started_at)


def _parse_standard(
    source_path: Path,
    options: ParseOptions,
    started_at: float,
) -> ParsedContent:
    try:
        result = options.markitdown_factory().convert(source_path)
    except Exception as error:  # pragma: no cover - converter errors vary.
        raise ParseFailure("parser_failed", f"MarkItDown failed: {error}") from error

    content = result.text_content.strip()
    return ParsedContent(
        markdown=f"{content}\n" if content else "",
        diagnostics=_diagnostics(
            name="markitdown",
            version=_package_version("markitdown"),
            elapsed_ms=_elapsed_ms(started_at),
            empty_result=not content,
        ),
    )


def _parse_with_visual_plugin(
    source_path: Path,
    content_type: str,
    options: ParseOptions,
    started_at: float,
) -> ParsedContent:
    collector = VisualArtifactCollector()
    try:
        markitdown = MarkItDown(enable_builtins=False, enable_plugins=False)
        register_visual_converters(
            markitdown,
            visual_client=options.get_visual_client(),
            visual_model=options.visual_model,
            visual_item_timeout_seconds=options.visual_item_timeout_seconds,
            visual_job_deadline_seconds=options.visual_job_deadline_seconds,
            visual_max_concurrency=options.visual_max_concurrency,
            visual_artifact_collector=collector,
            visual_artifact_allowed_hosts=options.visual_artifact_allowed_hosts,
        )
        result = markitdown.convert(source_path)
        content = result.text_content.strip()
        warnings = _warnings_from_markdown(content)
    except Exception as error:
        return _visual_failure_result(
            source_path,
            options,
            started_at,
            collector,
            error,
        )

    visual_results = collector.results
    return ParsedContent(
        markdown=f"{content}\n" if content else "",
        diagnostics=_diagnostics(
            name="file2doc-markitdown-visual",
            version=visual_plugin_version,
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=bool(visual_results),
            remote_services_used=bool(visual_results),
            empty_result=not content,
            source_media_type=_media_type(content_type),
            visual_provider=VISUAL_PROVIDER,
            visual_model=options.visual_model,
            visual_result_schema_version=VISUAL_RESULT_SCHEMA_VERSION,
            visual_prompt_version=VISUAL_PROMPT_VERSION,
            visual_item_count=len(visual_results),
        ),
        warnings=warnings,
        visual_artifacts=collector.artifacts,
        visual_sources=collector.sources,
        visual_results=visual_results,
    )


def _visual_failure_result(
    source_path: Path,
    options: ParseOptions,
    started_at: float,
    collector: VisualArtifactCollector,
    error: Exception,
) -> ParsedContent:
    warning = {
        "severity": "warning",
        "code": "visual_item_failed",
        "message": f"Visual parsing was unavailable for {source_path.name}: {error}",
        "source_ref": {"type": "source"},
    }
    try:
        fallback = _parse_standard(source_path, options, started_at)
        content = fallback.markdown
    except ParseFailure:
        content = ""
    visual_results = collector.results
    return ParsedContent(
        markdown=content,
        diagnostics=_diagnostics(
            name="file2doc-markitdown-visual",
            version=visual_plugin_version,
            elapsed_ms=_elapsed_ms(started_at),
            ocr_used=bool(visual_results),
            remote_services_used=True,
            empty_result=not content.strip(),
            visual_provider=VISUAL_PROVIDER,
            visual_model=options.visual_model,
            visual_result_schema_version=VISUAL_RESULT_SCHEMA_VERSION,
            visual_prompt_version=VISUAL_PROMPT_VERSION,
            visual_item_count=len(visual_results),
        ),
        warnings=[warning],
        visual_artifacts=collector.artifacts,
        visual_sources=collector.sources,
        visual_results=visual_results,
    )


def _diagnostics(
    *,
    name: str,
    version: str | None,
    elapsed_ms: float,
    ocr_used: bool = False,
    remote_services_used: bool = False,
    empty_result: bool = False,
    **extra: Any,
) -> dict:
    return {
        "name": name,
        "version": version,
        "elapsed_ms": elapsed_ms,
        "ocr_used": ocr_used,
        "remote_services_used": remote_services_used,
        "empty_result": empty_result,
        **extra,
    }


def _warnings_from_markdown(markdown: str) -> list[dict]:
    lines = markdown.splitlines()
    warning_lines: list[str] = []
    in_warnings = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() in {"## warnings", "### visual warnings"}:
            in_warnings = True
            continue
        if in_warnings and stripped.startswith("##"):
            in_warnings = False
            continue
        if not in_warnings or not stripped:
            continue
        if stripped.lower() == "none.":
            in_warnings = False
            continue
        if not stripped.startswith(("- ", "* ")):
            in_warnings = False
            continue
        warning_lines.append(stripped[2:].strip())
    if not warning_lines:
        return []
    return [
        {
            "severity": "warning",
            "code": "visual_item_warning",
            "message": " ".join(dict.fromkeys(warning_lines)),
        }
    ]


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_visual_document(content_type: str) -> bool:
    return _media_type(content_type) in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }


def _env_float(name: str, default: float) -> float:
    configured = os.getenv(name)
    if configured is None:
        return default
    try:
        value = float(configured)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    configured = os.getenv(name)
    if configured is None:
        return default
    try:
        value = int(configured)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_hosts(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    configured = os.getenv(name)
    if configured is None:
        return default
    values = tuple(value.strip() for value in configured.split(",") if value.strip())
    return values or default
