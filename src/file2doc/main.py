from __future__ import annotations

import os
from pathlib import Path

from file2doc.app import create_app

DEFAULT_LOCAL_ASR_MODEL_DIR = Path("/data/file2doc/models/funasr")


def _auth_enabled() -> bool:
    return os.getenv("FILE2DOC_AUTH_ENABLED", "true").lower() not in {
        "0",
        "false",
        "no",
    }


def _configure_default_local_asr() -> None:
    if os.getenv("FILE2DOC_LOCAL_ASR_MODEL_DIR"):
        return
    if DEFAULT_LOCAL_ASR_MODEL_DIR.exists():
        os.environ["FILE2DOC_LOCAL_ASR_MODEL_DIR"] = str(DEFAULT_LOCAL_ASR_MODEL_DIR)


_configure_default_local_asr()

app = create_app(
    storage_root=os.getenv("FILE2DOC_STORAGE_ROOT", "/data/file2doc"),
    auth_enabled=_auth_enabled(),
    bearer_token=os.getenv("FILE2DOC_BEARER_TOKEN"),
)
