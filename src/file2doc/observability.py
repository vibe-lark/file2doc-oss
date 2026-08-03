from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path
import resource
import threading
import time
from typing import Any


_DURATION_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 900)
_PROVIDER_OUTCOMES = ("success", "429", "5xx", "timeout", "other")


class _HistogramState:
    def __init__(self) -> None:
        self.bucket_counts = [0 for _ in _DURATION_BUCKETS]
        self.count = 0
        self.sum = 0.0

    def observe(self, value: float) -> None:
        normalized = max(value, 0.0)
        for index, boundary in enumerate(_DURATION_BUCKETS):
            if normalized <= boundary:
                self.bucket_counts[index] += 1
        self.count += 1
        self.sum += normalized

    def snapshot(self) -> tuple[tuple[int, ...], int, float]:
        return tuple(self.bucket_counts), self.count, self.sum


class CapacityMetrics:
    """Process-local, low-cardinality Prometheus metrics for File2Doc capacity."""

    def __init__(
        self,
        *,
        job_configured_concurrency: int,
        visual_configured_concurrency: int,
    ) -> None:
        self.job_configured_concurrency = job_configured_concurrency
        self.visual_configured_concurrency = visual_configured_concurrency
        self._lock = threading.Lock()
        self._jobs_queued = 0
        self._jobs_active = 0
        self._jobs_completed = 0
        self._jobs_failed = 0
        self._job_durations = _HistogramState()
        self._job_queue_durations = _HistogramState()
        self._job_processing_durations = _HistogramState()
        self._visual_active = 0
        self._visual_peak = 0
        self._visual_attempts = 0
        self._visual_outcomes = {outcome: 0 for outcome in _PROVIDER_OUTCOMES}
        self._visual_durations = _HistogramState()
        self._usage_observed = 0
        self._usage_missing = 0
        self._tokens: dict[str, int] = defaultdict(int)

    def job_queued(self) -> float:
        with self._lock:
            self._jobs_queued += 1
        return time.monotonic()

    def job_started(self, *, queued_at: float) -> float:
        started_at = time.monotonic()
        with self._lock:
            self._jobs_queued = max(self._jobs_queued - 1, 0)
            self._jobs_active += 1
            self._job_queue_durations.observe(started_at - queued_at)
        return started_at

    def job_finished(
        self,
        *,
        succeeded: bool,
        duration_seconds: float,
        processing_duration_seconds: float,
    ) -> None:
        with self._lock:
            self._jobs_active = max(self._jobs_active - 1, 0)
            if succeeded:
                self._jobs_completed += 1
            else:
                self._jobs_failed += 1
            self._job_durations.observe(duration_seconds)
            self._job_processing_durations.observe(processing_duration_seconds)

    def visual_provider_started(self) -> float:
        with self._lock:
            self._visual_active += 1
            self._visual_peak = max(self._visual_peak, self._visual_active)
            self._visual_attempts += 1
        return time.monotonic()

    def visual_provider_finished(
        self,
        *,
        outcome: str,
        duration_seconds: float,
        usage: Any = None,
    ) -> None:
        normalized_outcome = outcome if outcome in _PROVIDER_OUTCOMES else "other"
        tokens = _extract_usage_tokens(usage)
        with self._lock:
            self._visual_active = max(self._visual_active - 1, 0)
            self._visual_outcomes[normalized_outcome] += 1
            self._visual_durations.observe(duration_seconds)
            if tokens is None:
                self._usage_missing += 1
            else:
                self._usage_observed += 1
                for token_type, value in tokens.items():
                    self._tokens[token_type] += value

    def render(self) -> str:
        with self._lock:
            snapshot = {
                "jobs_queued": self._jobs_queued,
                "jobs_active": self._jobs_active,
                "jobs_completed": self._jobs_completed,
                "jobs_failed": self._jobs_failed,
                "job_durations": self._job_durations.snapshot(),
                "job_queue_durations": self._job_queue_durations.snapshot(),
                "job_processing_durations": self._job_processing_durations.snapshot(),
                "visual_active": self._visual_active,
                "visual_peak": self._visual_peak,
                "visual_attempts": self._visual_attempts,
                "visual_outcomes": dict(self._visual_outcomes),
                "visual_durations": self._visual_durations.snapshot(),
                "usage_observed": self._usage_observed,
                "usage_missing": self._usage_missing,
                "tokens": dict(self._tokens),
            }
        lines: list[str] = []
        _gauge(lines, "file2doc_job_max_concurrency", self.job_configured_concurrency)
        _gauge(lines, "file2doc_jobs_queued", snapshot["jobs_queued"])
        _gauge(lines, "file2doc_jobs_active", snapshot["jobs_active"])
        _counter(lines, "file2doc_jobs_completed_total", snapshot["jobs_completed"])
        _counter(lines, "file2doc_jobs_failed_total", snapshot["jobs_failed"])
        _histogram(lines, "file2doc_job_duration_seconds", snapshot["job_durations"])
        _histogram(
            lines,
            "file2doc_job_queue_duration_seconds",
            snapshot["job_queue_durations"],
        )
        _histogram(
            lines,
            "file2doc_job_processing_duration_seconds",
            snapshot["job_processing_durations"],
        )
        _gauge(
            lines,
            "file2doc_visual_max_concurrency",
            self.visual_configured_concurrency,
        )
        _gauge(lines, "file2doc_visual_items_active", snapshot["visual_active"])
        _gauge(lines, "file2doc_visual_items_peak", snapshot["visual_peak"])
        _counter(lines, "file2doc_visual_attempts_total", snapshot["visual_attempts"])
        for outcome in _PROVIDER_OUTCOMES:
            _counter(
                lines,
                "file2doc_ark_requests_total",
                snapshot["visual_outcomes"][outcome],
                labels=f'outcome="{outcome}"',
                emit_type=outcome == _PROVIDER_OUTCOMES[0],
            )
        _histogram(
            lines,
            "file2doc_ark_request_duration_seconds",
            snapshot["visual_durations"],
        )
        _counter(
            lines,
            "file2doc_ark_usage_observed_total",
            snapshot["usage_observed"],
        )
        _counter(
            lines,
            "file2doc_ark_usage_missing_total",
            snapshot["usage_missing"],
        )
        for token_type in ("input", "output", "total"):
            _counter(
                lines,
                "file2doc_ark_tokens_total",
                snapshot["tokens"].get(token_type, 0),
                labels=f'type="{token_type}"',
                emit_type=token_type == "input",
            )
        _counter(lines, "file2doc_process_cpu_seconds_total", time.process_time())
        _gauge(lines, "file2doc_process_resident_memory_bytes", _resident_memory_bytes())
        return "\n".join(lines) + "\n"


def _extract_usage_tokens(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None

    def field(*names: str) -> int | None:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    input_tokens = field("input_tokens", "prompt_tokens")
    output_tokens = field("output_tokens", "completion_tokens")
    total_tokens = field("total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    values: dict[str, int] = {}
    if input_tokens is not None:
        values["input"] = input_tokens
    if output_tokens is not None:
        values["output"] = output_tokens
    if total_tokens is not None:
        values["total"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        values["total"] = input_tokens + output_tokens
    return values


def _gauge(lines: list[str], name: str, value: float) -> None:
    lines.extend((f"# TYPE {name} gauge", f"{name} {value}"))


def _counter(
    lines: list[str],
    name: str,
    value: float,
    *,
    labels: str = "",
    emit_type: bool = True,
) -> None:
    if emit_type:
        lines.append(f"# TYPE {name} counter")
    suffix = "{" + labels + "}" if labels else ""
    lines.append(f"{name}{suffix} {value}")


def _histogram(
    lines: list[str],
    name: str,
    snapshot: tuple[tuple[int, ...], int, float],
) -> None:
    bucket_counts, count, total = snapshot
    lines.append(f"# TYPE {name} histogram")
    for boundary, bucket_count in zip(_DURATION_BUCKETS, bucket_counts):
        lines.append(f'{name}_bucket{{le="{boundary:g}"}} {bucket_count}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {count}')
    lines.append(f"{name}_count {count}")
    lines.append(f"{name}_sum {total}")


def _resident_memory_bytes() -> int:
    statm = Path("/proc/self/statm")
    try:
        resident_pages = int(statm.read_text(encoding="ascii").split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss if os.uname().sysname == "Darwin" else rss * 1024)
