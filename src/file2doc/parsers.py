from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from markitdown import MarkItDown

from file2doc_markitdown_visual import __version__ as visual_plugin_version
from file2doc_markitdown_visual.plugin import (
    DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
    VisualArtifactCollector,
    VisualConcurrencyGate,
    VisualDiagnosticArtifact,
)


@dataclass(frozen=True)
class ParsedContent:
    markdown: str
    diagnostics: dict
    warnings: list[dict] = field(default_factory=list)
    visual_artifacts: tuple[VisualDiagnosticArtifact, ...] = ()


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
    visual_artifact_ttl_seconds: float = 3600
    visual_artifact_release_grace_seconds: float = 300
    visual_artifact_allowed_hosts: tuple[str, ...] = (
        DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS
    )
    visual_metrics: Any | None = None
    visual_concurrency_gate: VisualConcurrencyGate | None = None

    def __post_init__(self) -> None:
        gate = self.visual_concurrency_gate
        if gate is None:
            object.__setattr__(
                self,
                "visual_concurrency_gate",
                VisualConcurrencyGate(self.visual_max_concurrency),
            )
        elif gate.max_concurrency != max(1, int(self.visual_max_concurrency)):
            raise ValueError(
                "visual concurrency gate does not match configured concurrency"
            )

    def with_metrics(self, metrics: Any) -> "ParseOptions":
        return replace(self, visual_metrics=metrics)

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
            visual_max_concurrency=_env_int(
                "FILE2DOC_VISUAL_MAX_CONCURRENCY", 4
            ),
            visual_artifact_ttl_seconds=_env_float(
                "FILE2DOC_VISUAL_ARTIFACT_TTL_SECONDS", 3600
            ),
            visual_artifact_release_grace_seconds=_env_float(
                "FILE2DOC_VISUAL_ARTIFACT_RELEASE_GRACE_SECONDS", 300
            ),
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
            "timeout": self.visual_item_timeout_seconds,
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
        return _parse_with_visual_plugin(
            source_path,
            content_type,
            parse_options,
            started_at,
        )

    if _is_pdf(content_type) or _is_office(content_type):
        return _parse_with_visual_plugin(
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


def _parse_with_visual_plugin(
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
        artifact_collector = VisualArtifactCollector()
        result = MarkItDown(
            enable_builtins=False,
            enable_plugins=True,
            visual_client=options.get_visual_client(),
            visual_model=options.visual_model,
            visual_item_timeout_seconds=options.visual_item_timeout_seconds,
            visual_job_deadline_seconds=options.visual_job_deadline_seconds,
            visual_max_concurrency=options.visual_max_concurrency,
            visual_artifact_collector=artifact_collector,
            visual_artifact_allowed_hosts=options.visual_artifact_allowed_hosts,
            visual_metrics=options.visual_metrics,
            visual_concurrency_gate=options.visual_concurrency_gate,
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
        visual_artifacts=artifact_collector.artifacts,
    )
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


def _is_image(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in {
        "image/png",
        "image/jpeg",
        "image/jpg",
    }


def _is_pdf(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == "application/pdf"


def _is_office(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    hosts = tuple(
        dict.fromkeys(
            host.strip().lower().rstrip(".")
            for host in configured.split(",")
            if host.strip()
        )
    )
    return hosts or default
