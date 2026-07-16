"""File2Doc-owned visual parsing plugin for MarkItDown.

The image converter is derived from the image-conversion boundary in Microsoft's
``markitdown-ocr`` project. File2Doc maintains this implementation independently;
it is not a runtime wrapper around the upstream package.
"""

from .plugin import VisualParseError, register_converters

__version__ = "0.1.0"
__plugin_interface_version__ = 1

__all__ = [
    "VisualParseError",
    "__plugin_interface_version__",
    "__version__",
    "register_converters",
]
