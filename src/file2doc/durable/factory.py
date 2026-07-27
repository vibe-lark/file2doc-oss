from __future__ import annotations

import os
from pathlib import Path

from file2doc.durable.object_store import object_store_from_env
from file2doc.durable.repository import PostgresJobRepository
from file2doc.durable.runtime import DurableJobStore


def repository_from_env() -> PostgresJobRepository:
    database_url = os.environ.get("FILE2DOC_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "FILE2DOC_DATABASE_URL is required when FILE2DOC_RUNTIME=durable"
        )
    return PostgresJobRepository(database_url)


def durable_store_from_env() -> DurableJobStore:
    local_root = Path(
        os.environ.get("FILE2DOC_STORAGE_ROOT", "/data/file2doc")
    ) / "objects"
    return DurableJobStore(
        repository_from_env(),
        object_store_from_env(local_root=local_root),
    )
