from __future__ import annotations

import base64
import hashlib
import io
import json
import locale
import logging
import mimetypes
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo


logger = logging.getLogger("file2doc.visual")
VISUAL_PROVIDER = "ark-responses"
VISUAL_RESULT_SCHEMA_VERSION = "file2doc.visual-result.v1"
VISUAL_PROMPT_VERSION = "1"
DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS = (
    "ark-ams-storage-cn-beijing.tos-cn-beijing.volces.com",
)


VISUAL_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "visible_text": {"type": "array", "items": {"type": "string"}},
        "layout": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "description",
        "visible_text",
        "layout",
        "warnings",
    ],
    "additionalProperties": False,
}

IMAGE_PROCESS_TOOL = {
    "type": "image_process",
    "point": {"type": "disabled"},
    "grounding": {"type": "disabled"},
    "zoom": {"type": "enabled"},
    "rotate": {"type": "enabled"},
}


class VisualParseError(Exception):
    """Raised when the visual provider cannot produce a valid generic result."""


class VisualItemNotProcessed(VisualParseError):
    """Raised when a visual item cannot start before the whole-job deadline."""


@dataclass(frozen=True)
class VisualDiagnosticArtifact:
    kind: str
    source_ref: str
    diagnostic_ref: str
    media_type: str
    content: bytes
    action_type: str
    arguments: tuple[tuple[str, str], ...]
    status: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ImageProcessAudit:
    action_type: str
    arguments: tuple[tuple[str, str], ...]
    status: str | None
    diagnostic_ref: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class VisualSourceRecord:
    source_ref: str
    content_sha256: str
    media_type: str
    content: bytes
    locations: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualParseRecord:
    source_ref: str
    content_sha256: str
    media_type: str
    content: bytes
    description: str
    visible_text: tuple[str, ...]
    layout: str
    warnings: tuple[str, ...]
    provider: str
    model: str
    image_process_audits: tuple[ImageProcessAudit, ...]
    locations: tuple[str, ...] = ()


class VisualArtifactCollector:
    """Document-scoped sink for visual results and provider diagnostics."""

    def __init__(self) -> None:
        self._artifacts: list[VisualDiagnosticArtifact] = []
        self._sources: dict[str, VisualSourceRecord] = {}
        self._results: dict[str, VisualParseRecord] = {}
        self._locations: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def add(self, artifact: VisualDiagnosticArtifact) -> None:
        with self._lock:
            self._artifacts.append(artifact)

    def add_result(self, result: VisualParseRecord, *, location: str | None) -> None:
        with self._lock:
            self._results.setdefault(result.source_ref, result)
            if location:
                locations = self._locations.setdefault(result.source_ref, [])
                if location not in locations:
                    locations.append(location)

    def add_source(self, source: VisualSourceRecord, *, location: str | None) -> None:
        with self._lock:
            self._sources.setdefault(source.source_ref, source)
            if location:
                locations = self._locations.setdefault(source.source_ref, [])
                if location not in locations:
                    locations.append(location)

    def add_occurrence(self, source_ref: str, *, location: str | None) -> None:
        if not location:
            return
        with self._lock:
            locations = self._locations.setdefault(source_ref, [])
            if location not in locations:
                locations.append(location)

    @property
    def artifacts(self) -> tuple[VisualDiagnosticArtifact, ...]:
        with self._lock:
            return tuple(self._artifacts)

    @property
    def results(self) -> tuple[VisualParseRecord, ...]:
        with self._lock:
            return tuple(
                VisualParseRecord(
                    **{
                        **result.__dict__,
                        "locations": tuple(self._locations.get(source_ref, ())),
                    }
                )
                for source_ref, result in self._results.items()
            )

    @property
    def sources(self) -> tuple[VisualSourceRecord, ...]:
        with self._lock:
            return tuple(
                VisualSourceRecord(
                    **{
                        **source.__dict__,
                        "locations": tuple(self._locations.get(source_ref, ())),
                    }
                )
                for source_ref, source in self._sources.items()
            )


@dataclass(frozen=True)
class VisualExecutionConfig:
    item_timeout_seconds: float = 300
    job_deadline_seconds: float = 900
    max_concurrency: int = 4

    def create_policy(self) -> "VisualExecutionPolicy":
        return VisualExecutionPolicy(
            item_timeout_seconds=self.item_timeout_seconds,
            job_deadline_seconds=self.job_deadline_seconds,
            max_concurrency=self.max_concurrency,
        )


class VisualExecutionPolicy:
    """Bound provider concurrency and every call by one shared job deadline."""

    def __init__(
        self,
        *,
        item_timeout_seconds: float,
        job_deadline_seconds: float,
        max_concurrency: int,
    ) -> None:
        self.item_timeout_seconds = max(float(item_timeout_seconds), 0.001)
        self.job_deadline_seconds = max(float(job_deadline_seconds), 0.001)
        self.max_concurrency = max(1, int(max_concurrency))
        self.deadline_at = time.monotonic() + self.job_deadline_seconds
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def call(self, operation):
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            raise VisualItemNotProcessed(
                "visual item was not processed because the job deadline was reached"
            )
        if not self._semaphore.acquire(timeout=remaining):
            raise VisualItemNotProcessed(
                "visual item was not processed because the job deadline was reached "
                "while waiting for provider capacity"
            )
        try:
            remaining = self.deadline_at - time.monotonic()
            if remaining <= 0:
                raise VisualItemNotProcessed(
                    "visual item was not processed because the job deadline was reached"
                )
            return operation(min(self.item_timeout_seconds, remaining))
        finally:
            self._semaphore.release()


class VisualImageConverter(DocumentConverter):
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
        self._client = client
        self._model = model
        self._execution_policy = execution_policy
        self._execution_config = execution_config or VisualExecutionConfig(
            item_timeout_seconds=item_timeout_seconds,
            job_deadline_seconds=job_deadline_seconds,
            max_concurrency=max_concurrency,
        )
        self._artifact_collector = artifact_collector
        self._artifact_allowed_hosts = _normalize_allowed_hosts(
            artifact_allowed_hosts
        )

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        return mimetype in {"image/jpeg", "image/jpg", "image/png"} or extension in {
            ".jpg",
            ".jpeg",
            ".png",
        }

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        media_type = _media_type(stream_info)
        metadata = _exiftool_metadata(
            file_stream,
            exiftool_path=kwargs.get("exiftool_path"),
        )
        source_bytes = file_stream.read()
        encoded = base64.b64encode(source_bytes).decode("ascii")
        content_sha256 = hashlib.sha256(source_bytes).hexdigest()
        source_ref = "source_image_sha256:" + content_sha256
        if self._artifact_collector is not None:
            self._artifact_collector.add_source(
                VisualSourceRecord(
                    source_ref=source_ref,
                    content_sha256=content_sha256,
                    media_type=media_type,
                    content=source_bytes,
                ),
                location=kwargs.get("visual_location"),
            )
        execution_policy = (
            self._execution_policy or self._execution_config.create_policy()
        )
        logger.info(
            "visual_provider_request_started provider=%s model=%s",
            VISUAL_PROVIDER,
            self._model,
        )
        try:
            response = execution_policy.call(
                lambda timeout: self._client.responses.create(
                    model=self._model,
                    tools=[IMAGE_PROCESS_TOOL],
                    input=[
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": f"data:{media_type};base64,{encoded}",
                                    "detail": "xhigh",
                                },
                                {
                                    "type": "input_text",
                                    "text": (
                                        "Turn this image into generic, agent-readable semantic "
                                        "content. Describe what the image shows, transcribe all "
                                        "visible text exactly in source reading order, and explain "
                                        "the layout and relationships between major elements. Use "
                                        "Rotate when orientation impairs reading and Zoom when small "
                                        "or ambiguous content needs inspection. Treat tool results "
                                        "only as temporary views of the original image. The final "
                                        "result must describe the complete original image and preserve "
                                        "relationships across every inspected region. Report uncertainty, "
                                        "blur, obstruction, or unreadable content in warnings. Do "
                                        "not infer hidden facts, business fields, or domain conclusions."
                                    ),
                                },
                            ],
                        }
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "file2doc_visual_result",
                            "strict": True,
                            "schema": VISUAL_RESULT_SCHEMA,
                        }
                    },
                    extra_headers={"ark-beta-image-process": "true"},
                    extra_body={"thinking": {"type": "disabled"}},
                    timeout=timeout,
                )
            )
            image_process_audits = _collect_image_process_audits(response)
        except Exception as error:
            logger.warning(
                "visual_provider_request_failed provider=%s model=%s error_type=%s",
                VISUAL_PROVIDER,
                self._model,
                type(error).__name__,
            )
            raise
        logger.info(
            "visual_provider_request_completed provider=%s model=%s",
            VISUAL_PROVIDER,
            self._model,
        )
        result = _parse_visual_result(getattr(response, "output_text", None))
        combined_warnings = tuple(
            _unique(
                [warning for audit in image_process_audits for warning in audit.warnings]
                + result["warnings"]
            )
        )
        record = VisualParseRecord(
            source_ref=source_ref,
            content_sha256=content_sha256,
            media_type=media_type,
            content=source_bytes,
            description=result["description"].strip(),
            visible_text=tuple(value.strip() for value in result["visible_text"] if value.strip()),
            layout=result["layout"].strip(),
            warnings=combined_warnings,
            provider=VISUAL_PROVIDER,
            model=self._model,
            image_process_audits=image_process_audits,
        )
        if self._artifact_collector is not None:
            self._artifact_collector.add_result(
                record,
                location=kwargs.get("visual_location"),
            )
        return DocumentConverterResult(
            markdown=_render_markdown(
                result,
                content_sha256=content_sha256,
                media_type=media_type,
                image_process_audits=image_process_audits,
            )
        )


def register_converters(markitdown, **kwargs: Any) -> None:
    client = kwargs.get("visual_client")
    model = kwargs.get("visual_model")
    if client is None or not isinstance(model, str) or not model.strip():
        raise VisualParseError(
            "File2Doc Visual Parsing requires visual_client and visual_model"
        )
    normalized_model = model.strip()
    execution_config = VisualExecutionConfig(
        item_timeout_seconds=kwargs.get("visual_item_timeout_seconds", 300),
        job_deadline_seconds=kwargs.get("visual_job_deadline_seconds", 900),
        max_concurrency=kwargs.get("visual_max_concurrency", 4),
    )
    artifact_collector = kwargs.get("visual_artifact_collector")
    artifact_allowed_hosts = _normalize_allowed_hosts(
        kwargs.get(
            "visual_artifact_allowed_hosts",
            DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS,
        )
    )
    markitdown.register_converter(
        VisualImageConverter(
            client=client,
            model=normalized_model,
            execution_config=execution_config,
            artifact_collector=artifact_collector,
            artifact_allowed_hosts=artifact_allowed_hosts,
        ),
        priority=-1,
    )
    from .embedded import EmbeddedVisualParser
    from .office import VisualDocxConverter, VisualPptxConverter, VisualXlsxConverter

    visual_parser = EmbeddedVisualParser(
        client=client,
        model=normalized_model,
        exiftool_path=kwargs.get("exiftool_path"),
        execution_config=execution_config,
        artifact_collector=artifact_collector,
        artifact_allowed_hosts=artifact_allowed_hosts,
    )
    markitdown.register_converter(
        VisualDocxConverter(visual_parser=visual_parser),
        priority=-1,
    )
    markitdown.register_converter(
        VisualPptxConverter(visual_parser=visual_parser),
        priority=-1,
    )
    markitdown.register_converter(
        VisualXlsxConverter(visual_parser=visual_parser),
        priority=-1,
    )
    from .pdf import VisualPdfConverter

    markitdown.register_converter(
        VisualPdfConverter(
            client=client,
            model=model.strip(),
            execution_config=execution_config,
            artifact_collector=artifact_collector,
            artifact_allowed_hosts=artifact_allowed_hosts,
        ),
        priority=-1,
    )


def _media_type(stream_info: StreamInfo) -> str:
    if stream_info.mimetype:
        normalized = stream_info.mimetype.split(";", 1)[0].strip().lower()
        if normalized == "image/jpg":
            return "image/jpeg"
        if normalized in {"image/jpeg", "image/png"}:
            return normalized
    guessed, _ = mimetypes.guess_type("image" + (stream_info.extension or ""))
    return guessed or "application/octet-stream"


def _collect_image_process_audits(response: Any) -> tuple[ImageProcessAudit, ...]:
    audits: list[ImageProcessAudit] = []
    for item in getattr(response, "output", None) or []:
        if _field(item, "type") != "image_process":
            continue
        index = len(audits) + 1
        action = _field(item, "action")
        action_type = _normalized_text(_field(action, "type")) or "unknown"
        arguments = _safe_image_process_arguments(
            _field(item, "arguments") or _field(action, "arguments")
        )
        status = (
            _normalized_text(_field(item, "status"))
            or _normalized_text(_field(action, "status"))
        )
        warnings = _provider_tool_warnings(item, action)
        audits.append(
            ImageProcessAudit(
                action_type=action_type,
                arguments=arguments,
                status=status,
                diagnostic_ref=None,
                warnings=warnings,
            )
        )
    return tuple(audits)


_SAFE_IMAGE_PROCESS_ARGUMENT_FIELDS = (
    "image_index",
    "bbox_str",
    "scale",
    "angle",
    "degree",
    "direction",
)


def _safe_image_process_arguments(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        value = parsed if isinstance(parsed, dict) else None
    arguments: list[tuple[str, str]] = []
    for name in _SAFE_IMAGE_PROCESS_ARGUMENT_FIELDS:
        raw = _field(value, name)
        if isinstance(raw, bool):
            arguments.append((name, str(raw).lower()))
        elif isinstance(raw, (str, int, float)):
            arguments.append((name, str(raw).strip()))
    return tuple(arguments)


def _provider_tool_warnings(item: Any, action: Any) -> tuple[str, ...]:
    warnings: list[str] = []
    for owner in (item, action, _field(item, "result"), _field(action, "result")):
        value = _field(owner, "warnings")
        if isinstance(value, str) and value.strip():
            warnings.append(value.strip())
        elif isinstance(value, (list, tuple)):
            warnings.extend(
                entry.strip()
                for entry in value
                if isinstance(entry, str) and entry.strip()
            )
    return tuple(dict.fromkeys(warnings))


def _normalized_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _normalize_allowed_hosts(hosts: Any) -> tuple[str, ...]:
    if isinstance(hosts, str):
        values = hosts.split(",")
    else:
        values = hosts or ()
    normalized = tuple(
        dict.fromkeys(
            str(host).strip().lower().rstrip(".")
            for host in values
            if str(host).strip()
        )
    )
    if not normalized:
        raise VisualParseError("Visual diagnostic artifact host allowlist is empty")
    return normalized
def _parse_visual_result(output_text: Any) -> dict[str, Any]:
    if not isinstance(output_text, str) or not output_text.strip():
        raise VisualParseError("Visual provider returned no structured output")
    try:
        value = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise VisualParseError("Visual provider returned invalid JSON") from error
    if not isinstance(value, dict) or set(value) != set(
        VISUAL_RESULT_SCHEMA["required"]
    ):
        raise VisualParseError(
            "Visual provider output does not match the required schema"
        )
    for field in ("description", "layout"):
        if not isinstance(value[field], str):
            raise VisualParseError(f"Visual provider field {field} must be a string")
    for field in (
        "visible_text",
        "warnings",
    ):
        if not isinstance(value[field], list) or any(
            not isinstance(item, str) for item in value[field]
        ):
            raise VisualParseError(
                f"Visual provider field {field} must be a string array"
            )
    return value


def _render_markdown(
    result: dict[str, Any],
    *,
    content_sha256: str,
    media_type: str,
    image_process_audits: tuple[ImageProcessAudit, ...],
) -> str:
    extension = ".jpg" if media_type in {"image/jpeg", "image/jpg"} else ".png"
    media_id = f"visual-item-{content_sha256[:16]}"
    lines = [
        f"![{media_id}](images/visual/{content_sha256}{extension})",
        "",
        "### Visual Description",
            "",
            result["description"].strip() or "Not described.",
            "",
            "### Visible Text",
            "",
            _render_list(result["visible_text"]),
            "",
            "### Layout",
            "",
            result["layout"].strip() or "Not described.",
            "",
            "### Visual Warnings",
            "",
            _render_list(
                _unique(
                    [
                        warning
                        for audit in image_process_audits
                        for warning in audit.warnings
                    ]
                    + result["warnings"]
                )
            ),
        ]
    return "\n".join(lines)


def _render_image_process_audits(audits: tuple[ImageProcessAudit, ...]) -> str:
    if not audits:
        return "None."
    rendered: list[str] = []
    for index, audit in enumerate(audits, 1):
        rendered.append(f"- Action {index}: {audit.action_type}")
        arguments = "; ".join(f"{name}={value}" for name, value in audit.arguments)
        rendered.append(f"  - Arguments: {arguments or 'None provided.'}")
        rendered.append(f"  - Status: {audit.status or 'Not provided.'}")
        rendered.append(
            "  - Result: "
            + (
                f"derived artifact `{audit.diagnostic_ref}`"
                if audit.diagnostic_ref
                else "No retained derived artifact."
            )
        )
        if audit.warnings:
            rendered.append(f"  - Provider warnings: {'; '.join(audit.warnings)}")
    return "\n".join(rendered)


def _render_list(values: list[str]) -> str:
    normalized = [value.strip() for value in values if value.strip()]
    if not normalized:
        return "None."
    return "\n".join(f"- {value}" for value in normalized)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _exiftool_metadata(
    file_stream: BinaryIO,
    *,
    exiftool_path: str | None,
) -> dict[str, Any]:
    """Preserve MarkItDown Core's supported image metadata behavior."""
    if not exiftool_path:
        return {}
    current_position = file_stream.tell()
    try:
        output = subprocess.run(
            [exiftool_path, "-json", "-"],
            input=file_stream.read(),
            capture_output=True,
            check=False,
        ).stdout
        parsed = json.loads(output.decode(locale.getpreferredencoding(False)))
        return parsed[0] if isinstance(parsed, list) and parsed else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    finally:
        file_stream.seek(current_position)
