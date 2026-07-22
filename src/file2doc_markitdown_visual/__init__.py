"""File2Doc-owned visual parsing plugin for MarkItDown.

The image converter is derived from the image-conversion boundary in Microsoft's
``markitdown-ocr`` project. File2Doc maintains this implementation independently;
it is not a runtime wrapper around the upstream package.
"""

import json
from importlib.resources import files
from typing import Any

from .plugin import VisualParseError, register_converters

_PACKAGE_METADATA = json.loads(
    files(__package__).joinpath("PACKAGE-METADATA.json").read_text(encoding="utf-8")
)
__version__ = _PACKAGE_METADATA["version"]
__plugin_interface_version__ = 1


def package_metadata() -> dict[str, Any]:
    """Return the independently versioned embedded-package manifest."""
    return dict(_PACKAGE_METADATA)


__all__ = [
    "VisualParseError",
    "__plugin_interface_version__",
    "__version__",
    "package_metadata",
    "register_converters",
]
