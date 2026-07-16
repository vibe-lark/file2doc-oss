from __future__ import annotations

import io
from importlib.metadata import PackageNotFoundError, entry_points, version
import json
from pathlib import Path
from types import SimpleNamespace

from markitdown import MarkItDown, StreamInfo
from PIL import Image

from file2doc_markitdown_visual import (
    __version__,
    package_metadata,
    register_converters,
)


class _CapturingResponses:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.payload))


class _VisualClient:
    def __init__(self, payload: dict) -> None:
        self.responses = _CapturingResponses(payload)


def test_visual_plugin_installation_is_pinned_and_official_ocr_is_absent():
    plugin = next(
        entry_point
        for entry_point in entry_points(group="markitdown.plugin")
        if entry_point.name == "file2doc-markitdown-visual"
    )

    assert plugin.value == "file2doc_markitdown_visual"
    assert version("markitdown") == "0.1.2"
    assert __version__ == "0.2.0"
    metadata = package_metadata()
    assert metadata["name"] == "file2doc-markitdown-visual"
    assert metadata["distributionMode"] == "embedded-package-in-file2doc-wheel"
    assert metadata["markitdownCoreVersion"] == "0.1.2"
    assert metadata["runtimeComponents"] == {
        "pdfplumber": "0.11.x",
        "Pillow": ">=10",
        "python-docx": "markitdown[docx]==0.1.2",
        "python-pptx": "markitdown[pptx]==0.1.2",
        "openpyxl": "markitdown[xlsx]==0.1.2",
    }
    assert (
        metadata["upstreamDerivativeSource"]["commit"]
        == "e144e0a2be95b34df17433bac904e635f2c5e551"
    )
    try:
        version("markitdown-ocr")
    except PackageNotFoundError:
        pass
    else:  # pragma: no cover - protects the deployment environment.
        raise AssertionError("official markitdown-ocr must not be installed")

    sbom = json.loads(
        (Path(__file__).parents[1] / "src/file2doc_markitdown_visual/sbom.cdx.json")
        .read_text(encoding="utf-8")
    )
    assert sbom["metadata"]["component"]["version"] == "0.2.0"
    assert {component["name"] for component in sbom["components"]} >= {
        "markitdown",
        "pdfplumber",
        "Pillow",
        "python-docx",
        "python-pptx",
        "openpyxl",
    }


def test_markitdown_public_converter_renders_structured_visual_markdown():
    client = _VisualClient(
        {
            "description": "A laboratory instrument display with two readings.",
            "visibleText": ["P 47.1", "H 88.52"],
            "candidateNumericValues": ["47.1", "88.52"],
            "layout": "P is above H on the illuminated display.",
            "imageProcessActions": [
                "Zoomed the illuminated display before reading both values."
            ],
            "imageProcessWarnings": [],
            "warnings": [],
        }
    )
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(
        markitdown,
        visual_client=client,
        visual_model="ep-visual",
    )

    result = markitdown.convert_stream(
        io.BytesIO(_png_bytes()),
        stream_info=StreamInfo(filename="instrument.png", mimetype="image/png"),
    )

    assert "## Description" in result.markdown
    assert "A laboratory instrument display" in result.markdown
    assert "P 47.1" in result.markdown
    assert "H 88.52" in result.markdown
    assert "## Candidate Numeric Values" in result.markdown
    assert "## Layout" in result.markdown
    assert "## Image Process" in result.markdown
    assert "### Requested Capabilities" in result.markdown
    assert "Zoom: enabled" in result.markdown
    assert "Rotate: enabled" in result.markdown
    assert "### Provider-Reported Actions" in result.markdown
    assert "Zoomed the illuminated display" in result.markdown
    assert "### Provider-Reported Warnings" in result.markdown
    assert "## Warnings" in result.markdown

    request = client.responses.calls[0]
    assert request["model"] == "ep-visual"
    assert request["tools"] == [
        {
            "type": "image_process",
            "point": {"type": "disabled"},
            "grounding": {"type": "disabled"},
            "zoom": {"type": "enabled"},
            "rotate": {"type": "enabled"},
        }
    ]
    assert request["extra_headers"] == {"ark-beta-image-process": "true"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    image_input = request["input"][0]["content"][0]
    assert image_input["type"] == "input_image"
    assert image_input["detail"] == "xhigh"
    output_format = request["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert set(output_format["schema"]["required"]) == {
        "description",
        "visibleText",
        "candidateNumericValues",
        "layout",
        "imageProcessActions",
        "imageProcessWarnings",
        "warnings",
    }
    prompt = request["input"][0]["content"][1]["text"]
    assert "Use Rotate when orientation impairs reading" in prompt
    assert "Use Zoom on small or ambiguous regions" in prompt
    assert "report only Zoom or Rotate actions actually performed" in prompt


def test_visual_request_contract_verifies_active_segments_before_display_ocr():
    client = _VisualClient(
        {
            "description": "A segmented electronic display.",
            "visibleText": ["P 47.1", "H 88.52"],
            "candidateNumericValues": ["47.1", "88.52"],
            "layout": "Two illuminated readings.",
            "imageProcessActions": [],
            "imageProcessWarnings": [],
            "warnings": [],
        }
    )
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    markitdown.convert_stream(
        io.BytesIO(_png_bytes()),
        stream_info=StreamInfo(filename="display.png", mimetype="image/png"),
    )

    request = client.responses.calls[0]
    assert request["input"][0]["content"][0]["detail"] == "xhigh"
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True

    prompt = request["input"][0]["content"][1]["text"]
    assert "verify every character's illuminated state" in prompt
    assert "Treat unilluminated segment outlines as blank" in prompt
    assert "fewer characters than the physical digit positions" in prompt
    assert "transcribe only the illuminated characters" in prompt
    assert "use Zoom before finalizing the reading" in prompt


def test_standard_markitdown_plugin_describes_image_without_visible_text():
    client = _VisualClient(
        {
            "description": "A blue circular company logo on a white background.",
            "visibleText": [],
            "candidateNumericValues": [],
            "layout": "The logo is centered.",
            "imageProcessActions": [],
            "imageProcessWarnings": [],
            "warnings": [],
        }
    )
    markitdown = MarkItDown(
        enable_builtins=False,
        enable_plugins=True,
        visual_client=client,
        visual_model="ep-visual",
    )

    result = markitdown.convert_stream(
        io.BytesIO(_png_bytes()),
        stream_info=StreamInfo(filename="logo.png", mimetype="image/png"),
    )

    assert "A blue circular company logo" in result.markdown
    assert "## Visible Text\n\nNone." in result.markdown


def test_visual_converter_preserves_supported_exiftool_metadata(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(_png_bytes())
    exiftool = tmp_path / "exiftool"
    exiftool.write_text(
        '#!/bin/sh\ncat >/dev/null\nprintf \'[{"ImageSize":"16x16","Title":"Lab sample"}]\'\n',
        encoding="utf-8",
    )
    exiftool.chmod(0o755)
    client = _VisualClient(
        {
            "description": "A sample image.",
            "visibleText": [],
            "candidateNumericValues": [],
            "layout": "Centered.",
            "imageProcessActions": [],
            "imageProcessWarnings": [],
            "warnings": [],
        }
    )
    markitdown = MarkItDown(enable_builtins=False)
    register_converters(markitdown, visual_client=client, visual_model="ep-visual")

    result = markitdown.convert(image_path, exiftool_path=str(exiftool))

    assert "## Image Metadata" in result.markdown
    assert "- ImageSize: 16x16" in result.markdown
    assert "- Title: Lab sample" in result.markdown


def _png_bytes() -> bytes:
    image = Image.new("RGB", (16, 16), color=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
