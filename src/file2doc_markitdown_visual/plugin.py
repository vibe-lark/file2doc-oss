from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import locale
import logging
import mimetypes
import subprocess
import socket
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any, BinaryIO

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo
from PIL import Image, UnidentifiedImageError


logger = logging.getLogger("file2doc.visual")
VISUAL_PROVIDER = "ark-responses"
MAX_VISUAL_DIAGNOSTIC_BYTES = 20 * 1024 * 1024
DEFAULT_VISUAL_ARTIFACT_ALLOWED_HOSTS = (
    "ark-ams-storage-cn-beijing.tos-cn-beijing.volces.com",
)


VISUAL_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "visibleText": {"type": "array", "items": {"type": "string"}},
        "candidateNumericValues": {
            "type": "array",
            "items": {"type": "string"},
        },
        "layout": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "description",
        "visibleText",
        "candidateNumericValues",
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


class VisualArtifactCollector:
    """Document-scoped sink for provider-produced Image Process diagnostics."""

    def __init__(self) -> None:
        self._artifacts: list[VisualDiagnosticArtifact] = []
        self._lock = threading.Lock()

    def add(self, artifact: VisualDiagnosticArtifact) -> None:
        with self._lock:
            self._artifacts.append(artifact)

    @property
    def artifacts(self) -> tuple[VisualDiagnosticArtifact, ...]:
        with self._lock:
            return tuple(self._artifacts)


@dataclass(frozen=True)
class VisualExecutionConfig:
    item_timeout_seconds: float = 300
    job_deadline_seconds: float = 900
    max_concurrency: int = 4
    metrics_observer: Any | None = None

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
        metrics_observer: Any | None = None,
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
        self._metrics_observer = metrics_observer or self._execution_config.metrics_observer

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
        source_ref = "source_image_sha256:" + hashlib.sha256(source_bytes).hexdigest()
        execution_policy = (
            self._execution_policy or self._execution_config.create_policy()
        )
        def provider_item(timeout: float):
            started_at = (
                self._metrics_observer.visual_provider_started()
                if self._metrics_observer is not None
                else time.monotonic()
            )
            usage = None
            logger.info(
                "visual_provider_request_started provider=%s model=%s",
                VISUAL_PROVIDER,
                self._model,
            )
            try:
                response = self._client.responses.create(
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
                                        "Describe this image in detail and transcribe all visible "
                                        "text exactly. Before extracting, inspect orientation, "
                                        "small text, display regions, and ambiguous characters. "
                                        "Use Rotate when orientation impairs reading. Use Zoom on "
                                        "small or ambiguous regions before deciding their text or "
                                        "numeric value. Preserve repeated digits, decimal points, "
                                        "reading order, and the distinction between illuminated "
                                        "digits and unlit display placeholders. For electronic or "
                                        "segmented displays, verify every character's illuminated "
                                        "state. Treat unilluminated segment outlines as blank and "
                                        "never transcribe them. A reading may use fewer characters "
                                        "than the physical digit positions, so transcribe only the "
                                        "illuminated characters. If the illumination boundary is "
                                        "unclear, use Zoom before finalizing the reading. Return "
                                        "generic visual evidence only; "
                                        "do not infer business fields or domain conclusions."
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
                usage = getattr(response, "usage", None)
                remaining = max(
                    execution_policy.deadline_at - time.monotonic(),
                    0.001,
                )
                image_process_audits = _collect_image_process_audits(
                    response,
                    collector=self._artifact_collector,
                    source_ref=source_ref,
                    timeout=min(execution_policy.item_timeout_seconds, remaining),
                    allowed_hosts=self._artifact_allowed_hosts,
                )
                result = _parse_visual_result(getattr(response, "output_text", None))
            except Exception as error:
                if self._metrics_observer is not None:
                    self._metrics_observer.visual_provider_finished(
                        outcome=_provider_outcome(error),
                        duration_seconds=time.monotonic() - started_at,
                        usage=usage,
                    )
                logger.warning(
                    "visual_provider_request_failed provider=%s model=%s error_type=%s",
                    VISUAL_PROVIDER,
                    self._model,
                    type(error).__name__,
                )
                raise
            if self._metrics_observer is not None:
                self._metrics_observer.visual_provider_finished(
                    outcome="success",
                    duration_seconds=time.monotonic() - started_at,
                    usage=usage,
                )
            logger.info(
                "visual_provider_request_completed provider=%s model=%s",
                VISUAL_PROVIDER,
                self._model,
            )
            return result, image_process_audits

        result, image_process_audits = execution_policy.call(provider_item)
        return DocumentConverterResult(
            markdown=_render_markdown(
                result,
                metadata,
                provider=VISUAL_PROVIDER,
                model=self._model,
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
        metrics_observer=kwargs.get("visual_metrics"),
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
            metrics_observer=execution_config.metrics_observer,
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


def _provider_outcome(error: BaseException) -> str:
    status_code = getattr(error, "status_code", None)
    if status_code == 429:
        return "429"
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return "5xx"
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "timeout"
    return "other"


def _collect_image_process_audits(
    response: Any,
    *,
    collector: VisualArtifactCollector | None,
    source_ref: str,
    timeout: float,
    allowed_hosts: tuple[str, ...],
) -> tuple[ImageProcessAudit, ...]:
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
        result_url = _field(action, "result_image_url") or _field(
            item, "result_image_url"
        )
        diagnostic_ref = None
        if action_type in {"zoom", "rotate"} and isinstance(result_url, str):
            if collector is not None:
                try:
                    media_type, content = _download_provider_image(
                        result_url,
                        timeout=timeout,
                        allowed_hosts=allowed_hosts,
                    )
                except Exception as error:
                    raise VisualParseError(
                        f"Image Process {action_type} result could not be retained: {error}"
                    ) from error
                diagnostic_ref = f"{source_ref}:image_process:{index:04d}"
                collector.add(
                    VisualDiagnosticArtifact(
                        kind=f"image_process_{action_type}_result",
                        source_ref=source_ref,
                        diagnostic_ref=diagnostic_ref,
                        media_type=media_type,
                        content=content,
                        action_type=action_type,
                        arguments=arguments,
                        status=status,
                        warnings=warnings,
                    )
                )
        audits.append(
            ImageProcessAudit(
                action_type=action_type,
                arguments=arguments,
                status=status,
                diagnostic_ref=diagnostic_ref,
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


def _download_provider_image(
    url: str,
    *,
    timeout: float,
    allowed_hosts: tuple[str, ...],
) -> tuple[str, bytes]:
    if url.startswith("data:"):
        header, encoded = url.split(",", 1)
        if ";base64" not in header:
            raise ValueError("provider image data URL is not base64 encoded")
        media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        content = base64.b64decode(encoded, validate=True)
    else:
        _validate_provider_image_url(url, allowed_hosts=allowed_hosts)
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(url, timeout=timeout) as response:
                final_url = response.geturl()
                if final_url != url:
                    raise ValueError("provider image redirects are not allowed")
                _validate_provider_image_url(
                    final_url,
                    allowed_hosts=allowed_hosts,
                )
                media_type = response.headers.get_content_type()
                content = response.read(MAX_VISUAL_DIAGNOSTIC_BYTES + 1)
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise ValueError("provider image redirects are not allowed") from error
            raise
    if not content:
        raise ValueError("provider image result is empty")
    if len(content) > MAX_VISUAL_DIAGNOSTIC_BYTES:
        raise ValueError("provider image result exceeds the diagnostic size limit")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("provider image result is not a valid raster image") from error
    detected_media_type = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
    }.get(image_format)
    if detected_media_type is None:
        raise ValueError(f"unsupported provider image format: {image_format or 'unknown'}")
    if media_type not in {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "application/octet-stream",
    }:
        raise ValueError(f"unsupported provider image media type: {media_type}")
    return detected_media_type, content


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


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


def _validate_provider_image_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
) -> None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https":
        raise ValueError("provider image URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider image URL must not contain credentials")
    if parsed.port not in {None, 443}:
        raise ValueError("provider image URL must use the default HTTPS port")
    if hostname not in allowed_hosts:
        raise ValueError("provider image host is not in the allowlist")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as error:
        raise ValueError("provider image host could not be resolved") from error
    if not addresses:
        raise ValueError("provider image host resolved to no addresses")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("provider image host returned an invalid address") from error
        if not address.is_global:
            raise ValueError("provider image host resolves to a non-public address")


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
        "visibleText",
        "candidateNumericValues",
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
    metadata: dict[str, Any],
    *,
    provider: str,
    model: str,
    image_process_audits: tuple[ImageProcessAudit, ...],
) -> str:
    lines = [
        "# Visual Analysis",
        "",
    ]
    supported_metadata = [
        (field, metadata[field])
        for field in (
            "ImageSize",
            "Title",
            "Caption",
            "Description",
            "Keywords",
            "Artist",
            "Author",
            "DateTimeOriginal",
            "CreateDate",
            "GPSPosition",
        )
        if field in metadata
    ]
    if supported_metadata:
        lines.extend(
            [
                "## Image Metadata",
                "",
                *(f"- {field}: {value}" for field, value in supported_metadata),
                "",
            ]
        )
    lines.extend(
        [
            "## Visual Parsing Runtime",
            "",
            f"- Provider: {provider}",
            f"- Model: {model}",
            "",
            "## Description",
            "",
            result["description"].strip() or "Not described.",
            "",
            "## Visible Text",
            "",
            _render_list(result["visibleText"]),
            "",
            "## Candidate Numeric Values",
            "",
            _render_list(result["candidateNumericValues"]),
            "",
            "## Layout",
            "",
            result["layout"].strip() or "Not described.",
            "",
            "## Image Process",
            "",
            "### Requested Capabilities",
            "",
            "- Zoom: enabled",
            "- Rotate: enabled",
            "- Point: disabled",
            "- Grounding: disabled",
            "",
            "### Provider Tool Calls",
            "",
            _render_image_process_audits(image_process_audits),
            "",
            "## Warnings",
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
    )
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
