from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path

import pypdfium2 as pdfium


AGENT_PAGE_IMAGE_DPI = 144
AGENT_THUMBNAIL_MAX_EDGE = 512
ALLOWED_PAGE_IMAGE_DPI = {144, 216, 288}


def is_pdf_source(source_path: Path, content_type: str) -> bool:
    return (
        content_type.split(";", 1)[0] == "application/pdf"
        or source_path.suffix.lower() == ".pdf"
    )


def render_pdf_visual_assets(
    source_path: Path,
    result_root: Path,
    *,
    new_artifact_id: Callable[[], str],
    page_image_dpi: int = AGENT_PAGE_IMAGE_DPI,
    thumbnail_max_edge: int = AGENT_THUMBNAIL_MAX_EDGE,
    visual_results=(),
    visual_configured: bool = False,
) -> tuple[list[dict], list[dict], dict[str, dict]]:
    document = pdfium.PdfDocument(source_path)
    page_count = len(document)
    page_index = [_page_index_entry(page_number) for page_number in range(1, page_count + 1)]
    if page_count == 0:
        return page_index, [], {}
    media_index: list[dict] = []
    artifacts: dict[str, dict] = {}
    for page_number in range(1, page_count + 1):
        page_image_path = Path(f"images/pages/page_{page_number:03d}.png")
        thumbnail_path = Path(f"images/thumbs/page_{page_number:03d}.jpg")
        ocr_path = Path(f"ocr/page_{page_number:03d}.json")
        (result_root / page_image_path).parent.mkdir(parents=True, exist_ok=True)
        (result_root / thumbnail_path).parent.mkdir(parents=True, exist_ok=True)
        (result_root / ocr_path).parent.mkdir(parents=True, exist_ok=True)

        image = document[page_number - 1].render(
            scale=page_image_dpi / 72
        ).to_pil()
        image.save(result_root / page_image_path)

        thumbnail = image.copy()
        thumbnail.thumbnail((thumbnail_max_edge, thumbnail_max_edge))
        if thumbnail.mode != "RGB":
            thumbnail = thumbnail.convert("RGB")
        thumbnail.save(result_root / thumbnail_path, format="JPEG")
        (result_root / ocr_path).write_text(
            json.dumps(
                _visual_text_sidecar(
                    page_number,
                    visual_results,
                    visual_configured=visual_configured,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        page_image_artifact_id = new_artifact_id()
        thumbnail_artifact_id = new_artifact_id()
        ocr_artifact_id = new_artifact_id()
        page_image_id = f"page-{page_number}-image"
        thumbnail_id = f"page-{page_number}-thumb"
        media_index.extend(
            [
                {
                    "id": page_image_id,
                    "kind": "page_image",
                    "page": page_number,
                    "path": page_image_path.as_posix(),
                    "artifact_id": page_image_artifact_id,
                    "media_type": "image/png",
                    "thumbnail_id": thumbnail_id,
                    "thumbnail_path": thumbnail_path.as_posix(),
                    "thumbnail_artifact_id": thumbnail_artifact_id,
                    "source_ref": {"type": "page", "page": page_number},
                    "derived": False,
                },
                {
                    "id": thumbnail_id,
                    "kind": "thumbnail",
                    "page": page_number,
                    "path": thumbnail_path.as_posix(),
                    "artifact_id": thumbnail_artifact_id,
                    "media_type": "image/jpeg",
                    "source_ref": {"type": "page", "page": page_number},
                    "derived": False,
                },
            ]
        )
        page_index[page_number - 1] = page_index[page_number - 1] | {
            "page_image_id": page_image_id,
            "thumbnail_id": thumbnail_id,
            "ocr_artifact_id": ocr_artifact_id,
            "ocr_path": ocr_path.as_posix(),
        }
        artifacts.update(
            {
                page_image_artifact_id: {
                    "artifact_id": page_image_artifact_id,
                    "kind": "page_image",
                    "path": page_image_path.as_posix(),
                    "media_type": "image/png",
                    "page": page_number,
                },
                thumbnail_artifact_id: {
                    "artifact_id": thumbnail_artifact_id,
                    "kind": "thumbnail",
                    "path": thumbnail_path.as_posix(),
                    "media_type": "image/jpeg",
                    "page": page_number,
                },
                ocr_artifact_id: {
                    "artifact_id": ocr_artifact_id,
                    "kind": "ocr_sidecar",
                    "path": ocr_path.as_posix(),
                    "media_type": "application/json; charset=utf-8",
                    "page": page_number,
                },
            }
        )
    document.close()
    return page_index, media_index, artifacts


def _visual_text_sidecar(
    page_number: int,
    visual_results,
    *,
    visual_configured: bool,
) -> dict:
    page_prefix = f"page {page_number}"
    matching = [
        result
        for result in visual_results
        if any(location.lower().startswith(page_prefix) for location in result.locations)
    ]
    if matching:
        return {
            "page": page_number,
            "status": "completed",
            "engine": "vlm-visual-parsing",
            "text": "\n".join(
                text for result in matching for text in result.visible_text
            ),
            "blocks": [
                {
                    "source_ref": result.source_ref,
                    "locations": list(result.locations),
                    "description": result.description,
                    "visible_text": list(result.visible_text),
                    "layout": result.layout,
                    "warnings": list(result.warnings),
                }
                for result in matching
            ],
            "warnings": list(
                dict.fromkeys(
                    warning for result in matching for warning in result.warnings
                )
            ),
        }
    return {
        "page": page_number,
        "status": "not_used" if visual_configured else "not_configured",
        "engine": "vlm-visual-parsing" if visual_configured else None,
        "text": "",
        "blocks": [],
        "warnings": (
            []
            if visual_configured
            else ["Visual Parsing is not configured for this deployment."]
        ),
    }


def render_pdf_page_image(
    source_path: Path,
    result_root: Path,
    *,
    page_number: int,
    dpi: int,
    artifact_id: str,
) -> tuple[dict, dict]:
    document = pdfium.PdfDocument(source_path)
    page_count = len(document)
    if page_number < 1 or page_number > page_count:
        raise PageRenderError("invalid_page")

    image_path = Path(f"images/pages/page_{page_number:03d}_{dpi}dpi.png")
    (result_root / image_path).parent.mkdir(parents=True, exist_ok=True)

    page = document[page_number - 1]
    bitmap = page.render(scale=dpi / 72)
    image = bitmap.to_pil()
    image.save(result_root / image_path)

    media_id = f"page-{page_number}-image-{dpi}dpi"
    media = {
        "id": media_id,
        "kind": "page_image",
        "page": page_number,
        "dpi": dpi,
        "path": image_path.as_posix(),
        "artifact_id": artifact_id,
        "media_type": "image/png",
        "source_ref": {"type": "page", "page": page_number},
        "derived": True,
    }
    artifact = {
        "artifact_id": artifact_id,
        "kind": "page_image",
        "path": image_path.as_posix(),
        "media_type": "image/png",
        "page": page_number,
        "dpi": dpi,
        "derived": True,
    }
    return media, artifact


class PageRenderError(ValueError):
    pass


def _page_index_entry(page_number: int) -> dict:
    return {
        "page": page_number,
        "source_unit": "page",
        "source_unit_index": page_number,
        "parse_status": "parsed",
    }
