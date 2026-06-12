from __future__ import annotations

import os

from file2doc.app import create_app


def _auth_enabled() -> bool:
    return os.getenv("FILE2DOC_AUTH_ENABLED", "true").lower() not in {
        "0",
        "false",
        "no",
    }


app = create_app(
    storage_root=os.getenv("FILE2DOC_STORAGE_ROOT", "/data/file2doc"),
    auth_enabled=_auth_enabled(),
    bearer_token=os.getenv("FILE2DOC_BEARER_TOKEN"),
)
