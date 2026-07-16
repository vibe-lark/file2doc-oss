from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from markitdown import StreamInfo
from PIL import Image, UnidentifiedImageError

from .plugin import VisualExecutionConfig, VisualExecutionPolicy, VisualImageConverter


@dataclass(frozen=True)
class EmbeddedVisualResult:
    markdown: str
    warning: str | None = None


class EmbeddedVisualParser:
    """Create document-scoped visual parsing sessions."""

    def __init__(
        self,
        *,
        client,
        model: str,
        exiftool_path: str | None = None,
        execution_policy: VisualExecutionPolicy | None = None,
        execution_config: VisualExecutionConfig | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._exiftool_path = exiftool_path
        self._execution_policy = execution_policy
        self._execution_config = execution_config or VisualExecutionConfig()

    def new_session(self) -> "EmbeddedVisualSession":
        return EmbeddedVisualSession(
            client=self._client,
            model=self._model,
            exiftool_path=self._exiftool_path,
            execution_policy=(
                self._execution_policy or self._execution_config.create_policy()
            ),
        )


class EmbeddedVisualSession:
    """Deduplicate exact pixel assets only within one document conversion."""

    def __init__(
        self,
        *,
        client,
        model: str,
        exiftool_path: str | None,
        execution_policy: VisualExecutionPolicy | None = None,
    ) -> None:
        self._converter = VisualImageConverter(
            client=client,
            model=model,
            execution_policy=execution_policy,
        )
        self._exiftool_path = exiftool_path
        self._cache: dict[str, EmbeddedVisualResult] = {}

    def parse(
        self,
        image_bytes: bytes,
        *,
        content_type: str | None,
    ) -> EmbeddedVisualResult:
        content_hash = hashlib.sha256(image_bytes).hexdigest()
        cached = self._cache.get(content_hash)
        if cached is not None:
            return cached

        try:
            normalized_bytes, normalized_content_type = _normalize_raster(
                image_bytes,
                content_type=content_type,
            )
        except UnsupportedEmbeddedVisual as error:
            result = EmbeddedVisualResult(markdown="", warning=str(error))
            self._cache[content_hash] = result
            return result

        stream_info = StreamInfo(
            mimetype=normalized_content_type,
            extension=_extension_for(normalized_content_type),
        )
        try:
            converted = self._converter.convert(
                io.BytesIO(normalized_bytes),
                stream_info,
                exiftool_path=self._exiftool_path,
            )
            result = EmbeddedVisualResult(markdown=converted.markdown)
        except Exception as error:
            result = EmbeddedVisualResult(
                markdown="",
                warning=f"{type(error).__name__}: {error}",
            )
        self._cache[content_hash] = result
        return result


def render_embedded_visual(result: EmbeddedVisualResult, *, location: str) -> str:
    lines = [f"### Embedded Image: {location}", ""]
    if result.warning is not None:
        lines.append(f"> Warning: visual parsing failed at {location}: {result.warning}")
    else:
        lines.append(_demote_headings(result.markdown, levels=3))
    return "\n".join(lines).strip()


def _demote_headings(markdown: str, *, levels: int) -> str:
    prefix = "#" * levels
    return "\n".join(
        f"{prefix}{line}" if line.startswith("#") else line
        for line in markdown.splitlines()
    )


def _extension_for(content_type: str | None) -> str | None:
    if content_type == "image/png":
        return ".png"
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return None


class UnsupportedEmbeddedVisual(Exception):
    """Raised when an embedded asset is not a Pillow-readable raster image."""


def _normalize_raster(
    image_bytes: bytes,
    *,
    content_type: str | None,
) -> tuple[bytes, str]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = (image.format or "").upper()
            if image_format == "PNG":
                return image_bytes, "image/png"
            if image_format in {"JPEG", "JPG"}:
                return image_bytes, "image/jpeg"

            image.seek(0)
            bands = image.getbands()
            normalized = image.convert(
                "RGBA" if "A" in bands or "transparency" in image.info else "RGB"
            )
            output = io.BytesIO()
            normalized.save(output, format="PNG")
            return output.getvalue(), "image/png"
    except (UnidentifiedImageError, OSError, ValueError) as error:
        media_type = content_type or "unknown media type"
        raise UnsupportedEmbeddedVisual(
            f"Unsupported non-raster embedded visual asset ({media_type})"
        ) from error
