from __future__ import annotations

from pathlib import Path

import pytest


TEST_FILES_ROOT = Path("test-files")


def sample_file(filename: str) -> Path:
    path = TEST_FILES_ROOT / filename
    if not path.is_file():
        pytest.skip(f"local test file is missing: {path}")
    return path
