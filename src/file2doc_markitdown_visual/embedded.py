from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from markitdown import StreamInfo

from .plugin import VisualImageConverter


@dataclass(frozen=True)
class EmbeddedVisualResult:
    markdown: str
    warning: str | None = None


class EmbeddedVisualParser:
    """Parse unique embedded pixel assets through the visual image converter."""

    def __init__(
        self,
        *,
        client,
        model: str,
        exiftool_path: str | None = None,
    ) -> None:
        self._converter = VisualImageConverter(client=client, model=model)
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

        stream_info = StreamInfo(
            mimetype=content_type or "application/octet-stream",
            extension=_extension_for(content_type),
        )
        try:
            converted = self._converter.convert(
                io.BytesIO(image_bytes),
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
