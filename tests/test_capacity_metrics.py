from __future__ import annotations

import threading
import time
import io
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from file2doc.app import create_app
from file2doc.parsers import ParseOptions
from file2doc_markitdown_visual.plugin import VisualImageConverter
from markitdown import StreamInfo
from PIL import Image


def _metric_value(body: str, name: str, labels: str = "") -> float:
    prefix = name + ("{" + labels + "}" if labels else "") + " "
    line = next(line for line in body.splitlines() if line.startswith(prefix))
    return float(line.removeprefix(prefix))


def test_metrics_is_cluster_scrapeable_without_business_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE2DOC_MAX_CONCURRENT_JOBS", "3")
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=True, bearer_token="secret")
    )

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert _metric_value(response.text, "file2doc_job_max_concurrency") == 3
    assert "file2doc_jobs_queued" in response.text
    assert "file2doc_jobs_active" in response.text
    assert "file2doc_jobs_completed_total" in response.text
    assert "file2doc_jobs_failed_total" in response.text
    assert "file2doc_job_duration_seconds" in response.text
    assert "file2doc_process_cpu_seconds_total" in response.text
    assert "file2doc_process_resident_memory_bytes" in response.text
    assert "secret" not in response.text


def test_capabilities_exposes_capacity_and_safe_provider_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE2DOC_MAX_CONCURRENT_JOBS", "5")
    secret = "provider-key-must-stay-private"
    options = ParseOptions(
        visual_client=SimpleNamespace(responses=SimpleNamespace()),
        visual_model="ep-capacity-visual",
        visual_api_key=secret,
        visual_base_url="https://private-provider.example.test/v3",
        visual_max_concurrency=3,
    )
    client = TestClient(
        create_app(storage_root=tmp_path, auth_enabled=False, parse_options=options)
    )

    capabilities = client.get("/capabilities").json()

    assert capabilities["job_max_concurrency"] == 5
    assert capabilities["visual_max_concurrency"] == 3
    assert capabilities["provider_roles"] == {
        "visual_understanding": {
            "endpoint_role": "visual",
            "provider": "ark-responses",
            "model": "ep-capacity-visual",
        }
    }
    serialized = json.dumps(capabilities)
    assert secret not in serialized
    assert "private-provider.example.test" not in serialized


def test_job_metrics_follow_the_real_job_semaphore(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    active = 0
    active_lock = threading.Lock()

    import file2doc.store as store_module

    original_parse = store_module.parse_content_markdown

    def blocked_parse(source_path, content_type, options=None):
        nonlocal active
        with active_lock:
            active += 1
            entered.set()
        release.wait(timeout=3)
        try:
            return original_parse(source_path, content_type, options)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(store_module, "parse_content_markdown", blocked_parse)
    monkeypatch.setenv("FILE2DOC_MAX_CONCURRENT_JOBS", "1")

    with TestClient(create_app(storage_root=tmp_path, auth_enabled=False)) as client:
        created_jobs = []
        for index in range(3):
            response = client.post(
                "/parse-jobs/upload",
                files={"file": (f"source-{index}.txt", b"hello", "text/plain")},
            )
            assert response.status_code == 201
            created_jobs.append(response.json())

        assert entered.wait(timeout=1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            body = client.get("/metrics").text
            if (
                _metric_value(body, "file2doc_jobs_active") == 1
                and _metric_value(body, "file2doc_jobs_queued") == 2
            ):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(body)

        states = [client.get(job["poll_url"]).json() for job in created_jobs]
        assert [state["status"] for state in states].count("processing") == 1
        assert [state["status"] for state in states].count("queued") == 2
        processing = next(state for state in states if state["status"] == "processing")
        assert processing["stage"] == "processing"
        assert processing["started_at"]

        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            body = client.get("/metrics").text
            if _metric_value(body, "file2doc_jobs_completed_total") == 3:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(body)

        assert _metric_value(body, "file2doc_jobs_active") == 0
        assert _metric_value(body, "file2doc_jobs_queued") == 0
        assert _metric_value(body, "file2doc_jobs_failed_total") == 0


def test_visual_provider_metrics_track_real_parallel_calls_and_usage(
    tmp_path, monkeypatch
):
    entered = 0
    lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "description": "generic image",
                "visibleText": [],
                "candidateNumericValues": [],
                "layout": "centered",
                "warnings": [],
            }
        ),
        output=[],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18),
    )

    class Responses:
        def create(self, **kwargs):
            nonlocal entered
            with lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            assert release.wait(timeout=10)
            return response

    options = ParseOptions(
        visual_client=SimpleNamespace(responses=Responses()),
        visual_model="capacity-model",
        visual_max_concurrency=1,
    )
    app = create_app(storage_root=tmp_path, auth_enabled=False, parse_options=options)
    converter = VisualImageConverter(
        client=options.visual_client,
        model=options.visual_model,
        max_concurrency=1,
        metrics_observer=app.state.file2doc_metrics,
    )
    image = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(image, format="PNG")
    failures = []

    def convert():
        try:
            converter.convert(
                io.BytesIO(image.getvalue()),
                StreamInfo(filename="private.png", mimetype="image/png"),
            )
        except Exception as error:  # pragma: no cover - asserted below.
            failures.append(error)

    threads = [threading.Thread(target=convert) for _ in range(2)]
    with TestClient(app) as client:
        for thread in threads:
            thread.start()

        assert both_entered.wait(timeout=5)
        body = client.get("/metrics").text
        assert _metric_value(body, "file2doc_visual_max_concurrency") == 1
        assert _metric_value(body, "file2doc_visual_items_active") == 2
        assert _metric_value(body, "file2doc_visual_items_peak") == 2
        assert _metric_value(body, "file2doc_visual_attempts_total") == 2
        assert "private-0.png" not in body
        assert "capacity-model" not in body

        release.set()
        for thread in threads:
            thread.join(timeout=3)
        assert not failures
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            body = client.get("/metrics").text
            if _metric_value(
                body,
                "file2doc_ark_requests_total",
                'outcome="success"',
            ) == 2:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(body)

        assert _metric_value(
            body,
            "file2doc_ark_tokens_total",
            'type="input"',
        ) == 22
        assert _metric_value(
            body,
            "file2doc_ark_tokens_total",
            'type="output"',
        ) == 14
        assert _metric_value(
            body,
            "file2doc_ark_tokens_total",
            'type="total"',
        ) == 36
        assert _metric_value(
            body, "file2doc_ark_usage_observed_total"
        ) == 2


def test_visual_provider_outcomes_are_bounded_and_missing_usage_is_explicit(tmp_path):
    class ProviderFailure(Exception):
        def __init__(self, status_code):
            super().__init__(f"provider failed with {status_code}")
            self.status_code = status_code

    cases = [
        (ProviderFailure(429), "429"),
        (ProviderFailure(503), "5xx"),
        (TimeoutError("provider timeout"), "timeout"),
        (RuntimeError("unexpected provider failure"), "other"),
    ]
    image = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(image, format="PNG")

    for index, (failure, outcome) in enumerate(cases):
        class Responses:
            def create(self, **kwargs):
                raise failure

        options = ParseOptions(
            visual_client=SimpleNamespace(responses=Responses()),
            visual_model="capacity-model",
        )
        app = create_app(
            storage_root=tmp_path / str(index),
            auth_enabled=False,
            parse_options=options,
        )
        converter = VisualImageConverter(
            client=options.visual_client,
            model=options.visual_model,
            metrics_observer=app.state.file2doc_metrics,
        )
        try:
            converter.convert(
                io.BytesIO(image.getvalue()),
                StreamInfo(filename="private.png", mimetype="image/png"),
            )
        except type(failure):
            pass
        else:  # pragma: no cover - the fake provider must fail.
            raise AssertionError("provider failure was not propagated")

        with TestClient(app) as client:
            body = client.get("/metrics").text
        assert _metric_value(
            body, "file2doc_ark_requests_total", f'outcome="{outcome}"'
        ) == 1
        assert _metric_value(body, "file2doc_ark_usage_missing_total") == 1
        assert _metric_value(body, "file2doc_visual_items_active") == 0
