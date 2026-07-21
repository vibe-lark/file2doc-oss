from fastapi.testclient import TestClient
from PIL import Image

from file2doc.app import create_app
from fixtures import sample_file


def test_completed_pdf_job_can_regenerate_first_page_image_at_higher_dpi(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    response = client.post(
        f"/parse-jobs/{job['job_id']}/assets/page-image",
        json={"page": 1, "dpi": 216},
    )

    assert response.status_code == 200
    asset = response.json()
    assert asset["artifact_id"]
    assert asset["media_id"] == "page-1-image-216dpi"
    assert asset["path"] == "images/pages/page_001_216dpi.png"
    assert asset["media_type"] == "image/png"
    assert asset["page"] == 1
    assert asset["dpi"] == 216
    assert asset["artifact_url"] == (
        f"/parse-jobs/{job['job_id']}/artifacts/{asset['artifact_id']}"
    )

    artifact_response = client.get(asset["artifact_url"])
    assert artifact_response.status_code == 200
    assert artifact_response.headers["content-type"] == "image/png"

    image_path = tmp_path / "page_001_216dpi.png"
    image_path.write_bytes(artifact_response.content)
    with Image.open(image_path) as image:
        assert image.width > 512
        assert image.height > 512

    manifest = client.get(job["result"]["manifest_url"]).json()
    media_by_id = {item["id"]: item for item in manifest["media_index"]}
    derived = media_by_id[asset["media_id"]]
    assert derived["artifact_id"] == asset["artifact_id"]
    assert derived["path"] == asset["path"]
    assert derived["kind"] == "page_image"
    assert derived["page"] == 1
    assert derived["dpi"] == 216
    assert derived["derived"] is True


def test_regenerated_page_image_rejects_unsupported_dpi(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()

    response = client.post(
        f"/parse-jobs/{created['job_id']}/assets/page-image",
        json={"page": 1, "dpi": 200},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_page_image_dpi"


def test_regenerated_page_image_rejects_invalid_page(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()

    response = client.post(
        f"/parse-jobs/{created['job_id']}/assets/page-image",
        json={"page": 0, "dpi": 216},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_page"
