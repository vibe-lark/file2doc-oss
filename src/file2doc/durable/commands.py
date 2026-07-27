from __future__ import annotations

import os
import signal
import socket
import threading
import time

from file2doc.durable.factory import repository_from_env
from file2doc.durable.object_store import object_store_from_env
from file2doc.durable.runtime import DurableWorker


def migrate() -> None:
    repository_from_env().migrate()


def worker() -> None:
    configured_kinds = os.environ.get("FILE2DOC_WORK_ITEM_KINDS") or os.environ.get(
        "FILE2DOC_WORK_ITEM_KIND", ""
    )
    kinds = tuple(kind.strip() for kind in configured_kinds.split(",") if kind.strip())
    supported_kinds = {
        "text_parse",
        "document_parse",
        "audio_parse",
        "video_asr",
        "video_frames",
        "assembly",
    }
    if not kinds or any(kind not in supported_kinds for kind in kinds):
        raise SystemExit(
            "FILE2DOC_WORK_ITEM_KIND must be one of "
            + ", ".join(sorted(supported_kinds))
        )
    poll_seconds = _positive_float("FILE2DOC_WORKER_POLL_SECONDS", 1.0)
    lease_seconds = _positive_int("FILE2DOC_WORK_ITEM_LEASE_SECONDS", 300)
    heartbeat_seconds = _positive_float(
        "FILE2DOC_WORK_ITEM_HEARTBEAT_SECONDS",
        min(30.0, lease_seconds / 3),
    )
    if heartbeat_seconds >= lease_seconds:
        raise SystemExit(
            "FILE2DOC_WORK_ITEM_HEARTBEAT_SECONDS must be shorter than the lease"
        )
    worker_id = os.environ.get("FILE2DOC_WORKER_ID") or (
        f"{socket.gethostname()}:{os.getpid()}:{'+'.join(kinds)}"
    )
    runner = DurableWorker(
        repository_from_env(),
        object_store_from_env(),
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    run_once = os.environ.get("FILE2DOC_WORKER_RUN_ONCE", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    draining = threading.Event()

    def begin_drain(_signum, _frame) -> None:
        draining.set()

    signal.signal(signal.SIGTERM, begin_drain)
    signal.signal(signal.SIGINT, begin_drain)
    while not draining.is_set():
        processed = False
        for kind in kinds:
            if draining.is_set():
                break
            processed = runner.run_once(kind) or processed
        if run_once:
            return
        if not processed:
            draining.wait(poll_seconds)


def _positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value
