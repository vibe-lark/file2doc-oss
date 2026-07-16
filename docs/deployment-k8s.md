# Kubernetes Deployment Template

This directory contains generic Kubernetes templates for a Linux/amd64 CPU-only
File2Doc deployment.

The manifests in `deploy/k8s/file2doc.yaml` are templates. Replace placeholder
values before applying them to a cluster:

- `image: file2doc:replace-me` must be set to the image tag produced by the
  deployment build.
- `FILE2DOC_BEARER_TOKEN: replace-me` must be replaced with an environment
  specific bearer token before use.
- PVC sizing and `storageClassName` can be adjusted for the target cluster.

## Runtime Shape

The deployment runs one `file2doc` container with:

- `uvicorn file2doc.main:app --host 0.0.0.0 --port 8000`
- `FILE2DOC_STORAGE_ROOT=/data/file2doc`
- `FILE2DOC_MAX_CONCURRENT_JOBS=2`
- `FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS=300`
- `FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS=900`
- `FILE2DOC_VISUAL_MAX_CONCURRENCY=4`
- `FILE2DOC_VISUAL_ARTIFACT_TTL_SECONDS=3600`
- `FILE2DOC_VISUAL_ARTIFACT_RELEASE_GRACE_SECONDS=300`
- `FILE2DOC_BEARER_TOKEN` loaded from the `file2doc-secret` Kubernetes Secret
- a PVC named `file2doc-data` mounted at `/data/file2doc`
- HTTP readiness and liveness probes for `/readyz` and `/healthz`
- pod `securityContext` sets UID/GID/fsGroup `1000` so the non-root container user can write PVC data
- deployment strategy is `Recreate` because a single ReadWriteOnce EBS volume cannot be mounted by old and new VCI Pods at the same time
- CPU and memory requests/limits only; no GPU resources

Upload requests only store the source file and enqueue the parse job. Parsing
runs in background workers inside the Pod, and callers retrieve final output by
polling the job endpoint and then downloading manifest/artifact URLs.

Visual provider model, endpoint, and credential values are loaded from
Kubernetes Secret keys named `FILE2DOC_VISUAL_MODEL`,
`FILE2DOC_VISUAL_BASE_URL`, and `FILE2DOC_VISUAL_API_KEY`; do not place their
values in this manifest.

## Apply

Review and edit the placeholders first, then apply:

```sh
kubectl apply -f deploy/k8s/file2doc.yaml
```
