from __future__ import annotations

from pathlib import Path

import yaml


MANIFEST_DIR = Path(__file__).resolve().parents[1] / "deploy" / "k8s"


def _load_manifests() -> list[dict]:
    assert MANIFEST_DIR.exists(), "deploy/k8s manifests directory is missing"
    manifests: list[dict] = []
    for manifest_path in sorted(MANIFEST_DIR.glob("*.yaml")):
        with manifest_path.open() as manifest_file:
            manifests.extend(
                doc
                for doc in yaml.safe_load_all(manifest_file)
                if isinstance(doc, dict)
            )
    assert manifests, "no Kubernetes YAML documents found"
    return manifests


def _by_kind(manifests: list[dict], kind: str) -> list[dict]:
    return [manifest for manifest in manifests if manifest.get("kind") == kind]


def test_k8s_manifests_define_cpu_only_file2doc_deployment() -> None:
    manifests = _load_manifests()

    secrets = _by_kind(manifests, "Secret")
    assert len(secrets) == 1
    secret = secrets[0]
    assert secret["metadata"]["name"] == "file2doc-secret"
    assert secret["stringData"]["FILE2DOC_BEARER_TOKEN"] == "replace-me"
    assert "FILE2DOC_OCR_MODEL" not in secret["stringData"]
    assert "FILE2DOC_OCR_API_KEY" not in secret["stringData"]
    assert "FILE2DOC_OCR_BASE_URL" not in secret["stringData"]

    pvcs = _by_kind(manifests, "PersistentVolumeClaim")
    assert len(pvcs) == 1
    pvc = pvcs[0]
    assert pvc["metadata"]["name"] == "file2doc-data"
    assert pvc["spec"]["storageClassName"] == "standard"

    deployments = _by_kind(manifests, "Deployment")
    assert len(deployments) == 1
    deployment = deployments[0]
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
    }
    assert "tolerations" not in pod_spec
    assert "imagePullSecrets" not in pod_spec
    assert pod_spec["securityContext"] == {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
    }
    assert pod_spec["initContainers"] == [
        {
            "name": "init-storage-permissions",
            "image": "file2doc:replace-me",
            "command": [
                "sh",
                "-c",
                "mkdir -p /data/file2doc && chown -R 1000:1000 /data/file2doc",
            ],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0},
            "volumeMounts": [
                {"name": "file2doc-data", "mountPath": "/data/file2doc"}
            ],
        }
    ]

    containers = pod_spec["containers"]
    assert len(containers) == 1
    container = containers[0]
    assert container["name"] == "file2doc"
    assert container["ports"] == [{"name": "http", "containerPort": 8000}]
    assert "--loop" in container["args"]
    assert "asyncio" in container["args"]
    assert "--http" in container["args"]
    assert "h11" in container["args"]
    assert container["env"] == [
        {"name": "FILE2DOC_STORAGE_ROOT", "value": "/data/file2doc"},
        {"name": "FILE2DOC_MAX_CONCURRENT_JOBS", "value": "2"},
        {"name": "FILE2DOC_VISUAL_ITEM_TIMEOUT_SECONDS", "value": "300"},
        {"name": "FILE2DOC_VISUAL_JOB_DEADLINE_SECONDS", "value": "900"},
        {"name": "FILE2DOC_VISUAL_MAX_CONCURRENCY", "value": "4"},
        {"name": "FILE2DOC_VISUAL_ARTIFACT_TTL_SECONDS", "value": "3600"},
        {
            "name": "FILE2DOC_VISUAL_ARTIFACT_RELEASE_GRACE_SECONDS",
            "value": "300",
        },
        {
            "name": "FILE2DOC_LOCAL_ASR_MODEL_DIR",
            "value": "/data/file2doc/models/sherpa-paraformer-zh",
        },
        {
            "name": "FILE2DOC_VISUAL_MODEL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "file2doc-secret",
                    "key": "FILE2DOC_VISUAL_MODEL",
                    "optional": True,
                }
            },
        },
        {
            "name": "FILE2DOC_VISUAL_API_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "file2doc-secret",
                    "key": "FILE2DOC_VISUAL_API_KEY",
                    "optional": True,
                }
            },
        },
        {
            "name": "FILE2DOC_VISUAL_BASE_URL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "file2doc-secret",
                    "key": "FILE2DOC_VISUAL_BASE_URL",
                    "optional": True,
                }
            },
        },
        {
            "name": "FILE2DOC_BEARER_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "file2doc-secret",
                    "key": "FILE2DOC_BEARER_TOKEN",
                }
            },
        },
    ]
    assert container["volumeMounts"] == [
        {"name": "file2doc-data", "mountPath": "/data/file2doc"}
    ]
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/readyz",
        "port": "http",
    }
    assert container["livenessProbe"]["httpGet"] == {
        "path": "/healthz",
        "port": "http",
    }
    resources = container["resources"]
    assert "nvidia.com/gpu" not in str(resources).lower()
    assert resources["requests"]["cpu"]
    assert resources["limits"]["cpu"]
    assert pod_spec["volumes"] == [
        {
            "name": "file2doc-data",
            "persistentVolumeClaim": {"claimName": "file2doc-data"},
        }
    ]

    services = _by_kind(manifests, "Service")
    assert len(services) == 1
    service = services[0]
    assert service["metadata"]["name"] == "file2doc"
    assert service["spec"]["ports"] == [
        {"name": "http", "port": 8000, "targetPort": "http"}
    ]


def test_public_ingress_exposes_file2doc_without_touching_ai_knowledge_service() -> None:
    manifests = _load_manifests()

    ingresses = [
        manifest
        for manifest in _by_kind(manifests, "Ingress")
        if manifest["metadata"]["name"] == "file2doc-public-ingress"
    ]
    assert len(ingresses) == 1
    ingress = ingresses[0]
    assert ingress["spec"]["ingressClassName"] == "nginx"
    assert ingress["metadata"]["annotations"] == {
        "kubernetes.io/ingress.class": "nginx",
        "nginx.ingress.kubernetes.io/use-regex": "true",
        "nginx.ingress.kubernetes.io/rewrite-target": "/$2",
        "nginx.ingress.kubernetes.io/proxy-body-size": "1024m",
        "nginx.ingress.kubernetes.io/proxy-buffering": "off",
        "nginx.ingress.kubernetes.io/proxy-request-buffering": "off",
        "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
        "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
    }
    assert ingress["spec"]["rules"] == [
        {
            "host": "file2doc.example.com",
            "http": {
                "paths": [
                    {
                        "path": "/file2doc(/|$)(.*)",
                        "pathType": "ImplementationSpecific",
                        "backend": {
                            "service": {
                                "name": "file2doc",
                                "port": {"number": 8000},
                            }
                        },
                    }
                ]
            },
        }
    ]
    assert "ai-knowledge" not in str(ingress)
    assert ingress["spec"]["tls"] == [
        {"hosts": ["file2doc.example.com"], "secretName": "file2doc-example-com"}
    ]
