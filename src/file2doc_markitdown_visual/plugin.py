from __future__ import annotations

import base64
import json
import locale
import mimetypes
import subprocess
from typing import Any, BinaryIO

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo


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
        "imageProcessActions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "imageProcessWarnings": {
            "type": "array",
            "items": {"type": "string"},
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "description",
        "visibleText",
        "candidateNumericValues",
        "layout",
        "imageProcessActions",
        "imageProcessWarnings",
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


class VisualImageConverter(DocumentConverter):
    def __init__(self, *, client: Any, model: str) -> None:
        self._client = client
        self._model = model

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
        encoded = base64.b64encode(file_stream.read()).decode("ascii")
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
                                "digits and unlit display placeholders. In "
                                "imageProcessActions, report only Zoom or Rotate actions "
                                "actually performed; use an empty array when neither was "
                                "used. Put tool-specific limitations in "
                                "imageProcessWarnings. Return generic visual evidence only; "
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
        )
        result = _parse_visual_result(getattr(response, "output_text", None))
        return DocumentConverterResult(markdown=_render_markdown(result, metadata))


def register_converters(markitdown, **kwargs: Any) -> None:
    client = kwargs.get("visual_client")
    model = kwargs.get("visual_model")
    if client is None or not isinstance(model, str) or not model.strip():
        raise VisualParseError(
            "File2Doc Visual Parsing requires visual_client and visual_model"
        )
    markitdown.register_converter(
        VisualImageConverter(client=client, model=model.strip()),
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
        "imageProcessActions",
        "imageProcessWarnings",
        "warnings",
    ):
        if not isinstance(value[field], list) or any(
            not isinstance(item, str) for item in value[field]
        ):
            raise VisualParseError(
                f"Visual provider field {field} must be a string array"
            )
    return value


def _render_markdown(result: dict[str, Any], metadata: dict[str, Any]) -> str:
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
            "### Provider-Reported Actions",
            "",
            _render_list(result["imageProcessActions"]),
            "",
            "### Provider-Reported Warnings",
            "",
            _render_list(result["imageProcessWarnings"]),
            "",
            "## Warnings",
            "",
            _render_list(_unique(result["imageProcessWarnings"] + result["warnings"])),
        ]
    )
    return "\n".join(lines)


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
