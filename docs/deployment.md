# Deployment

File2Doc runs as a FastAPI service with local filesystem storage and SQLite metadata under `FILE2DOC_STORAGE_ROOT`.

## Runtime Environment

Copy `.env.example` and set these values for the target environment:

```bash
FILE2DOC_STORAGE_ROOT=/data/file2doc
FILE2DOC_AUTH_ENABLED=true
FILE2DOC_BEARER_TOKEN=<long-random-token>
FILE2DOC_MAX_CONCURRENT_JOBS=2
FILE2DOC_LOCAL_ASR_MODEL_DIR=/data/file2doc/models/sherpa-paraformer-zh
FILE2DOC_VISUAL_MODEL=<ark-responses-vision-model>
FILE2DOC_VISUAL_API_KEY=<visual-provider-api-key>
FILE2DOC_VISUAL_BASE_URL=<ark-compatible-base-url>
FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS=300
FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS=900
FILE2DOC_VISUAL_MAX_CONCURRENCY=4
```

The `FILE2DOC_VISUAL_*` contract enables the File2Doc-owned MarkItDown visual plugin for standalone images, scanned pages, and embedded Office/PDF images. Each provider call is bounded by the item timeout, all calls share the whole-job deadline, and provider concurrency cannot exceed the configured maximum. Provider keys must come from environment or Kubernetes Secret injection and must never be written to manifests or evidence files.

The retired `FILE2DOC_OCR_*` aliases are intentionally unsupported. A deployment using them is misconfigured and will not activate Visual Parsing.

`POST /parse-jobs/upload` returns after the source file is stored and the job is queued. Parsing runs in an in-process background worker, capped by `FILE2DOC_MAX_CONCURRENT_JOBS`, so callers should poll the returned `poll_url` and then fetch result URLs after the job reaches a terminal status.

## Local Run

Install the package and run uvicorn:

```bash
python3 -m pip install -e '.[dev]'
FILE2DOC_STORAGE_ROOT=/tmp/file2doc \
FILE2DOC_AUTH_ENABLED=false \
uvicorn file2doc.main:app --host 127.0.0.1 --port 8000 --reload
```

With auth enabled, pass `Authorization: Bearer <FILE2DOC_BEARER_TOKEN>` to protected parse job endpoints. Probe endpoints are public.

## Health And Readiness

Health checks:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

`GET /healthz` returns `200` with:

```json
{"status":"ok","service":"file2doc"}
```

Readiness checks:

```bash
curl -fsS http://127.0.0.1:8000/readyz
```

`GET /readyz` returns `200` only when:

- `FILE2DOC_STORAGE_ROOT` is writable.
- The SQLite database can be opened.
- A SQLite readiness row can be written, read, and deleted.

The response also reports non-gating capability checks such as ASR model presence and ffmpeg availability. When a readiness dependency fails, `/readyz` returns `503` with per-check JSON status and error detail.

## Container Image

The `Dockerfile` is for a Linux/amd64 CPU-only runtime image. Build production
images in your normal CI or release builder.

Example command for the build machine:

```bash
docker build --platform linux/amd64 -t file2doc:<version> .
```

Kubernetes templates are available in `deploy/k8s/`.
