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
) -> tuple[list[dict], list[dict], dict[str, dict]]:
    document = pdfium.PdfDocument(source_path)
    page_count = len(document)
    page_index = [_page_index_entry(page_number) for page_number in range(1, page_count + 1)]
    if page_count == 0:
        return page_index, [], {}

    page_number = 1
    page_image_path = Path("images/pages/page_001.png")
    thumbnail_path = Path("images/thumbs/page_001.jpg")
    ocr_path = Path("ocr/page_001.json")
    (result_root / page_image_path).parent.mkdir(parents=True, exist_ok=True)
    (result_root / thumbnail_path).parent.mkdir(parents=True, exist_ok=True)
    (result_root / ocr_path).parent.mkdir(parents=True, exist_ok=True)

    page = document[0]
    bitmap = page.render(scale=page_image_dpi / 72)
    image = bitmap.to_pil()
    image.save(result_root / page_image_path)

    thumbnail = image.copy()
    thumbnail.thumbnail((thumbnail_max_edge, thumbnail_max_edge))
    if thumbnail.mode != "RGB":
        thumbnail = thumbnail.convert("RGB")
    thumbnail.save(result_root / thumbnail_path, format="JPEG")
    (result_root / ocr_path).write_text(
        json.dumps(_not_configured_ocr_sidecar(page_number), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    page_image_artifact_id = new_artifact_id()
    thumbnail_artifact_id = new_artifact_id()
    ocr_artifact_id = new_artifact_id()
    page_image_id = "page-1-image"
    thumbnail_id = "page-1-thumb"

    media_index = [
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
    page_index[0] = page_index[0] | {
        "page_image_id": page_image_id,
        "thumbnail_id": thumbnail_id,
        "ocr_artifact_id": ocr_artifact_id,
        "ocr_path": ocr_path.as_posix(),
    }
    artifacts = {
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
    return page_index, media_index, artifacts


def _not_configured_ocr_sidecar(page_number: int) -> dict:
    return {
        "page": page_number,
        "status": "not_configured",
        "engine": None,
        "text": "",
        "blocks": [],
        "warnings": ["OCR is not configured for this deployment."],
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
