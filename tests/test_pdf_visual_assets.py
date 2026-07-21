from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from file2doc.app import create_app
from fixtures import sample_file


def test_uploaded_pdf_manifest_includes_downloadable_page_visual_assets(tmp_path):
    sample = sample_file("雅迪渠道系统操作手册.pdf")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        data={"parser_profile": "agent", "retention": "short"},
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "completed"
    manifest = client.get(job["result"]["manifest_url"]).json()

    assert manifest["options"]["page_image_dpi"] == 144
    assert manifest["options"]["thumbnail_max_edge"] == 512

    first_page = manifest["page_index"][0]
    assert first_page["page"] == 1
    assert first_page["source_unit"] == "page"
    assert first_page["source_unit_index"] == 1
    assert first_page["parse_status"] == "parsed"

    media_by_id = {item["id"]: item for item in manifest["media_index"]}
    page_image = media_by_id[first_page["page_image_id"]]
    thumbnail = media_by_id[first_page["thumbnail_id"]]

    assert page_image["kind"] == "page_image"
    assert thumbnail["kind"] == "thumbnail"
    assert page_image["page"] == 1
    assert thumbnail["page"] == 1
    assert page_image["path"] == "images/pages/page_001.png"
    assert thumbnail["path"] == "images/thumbs/page_001.jpg"
    assert not Path(page_image["path"]).is_absolute()
    assert not Path(thumbnail["path"]).is_absolute()
    assert page_image["artifact_id"]
    assert thumbnail["artifact_id"]

    page_response = client.get(f"/parse-jobs/{job['job_id']}/artifacts/{page_image['artifact_id']}")
    thumb_response = client.get(f"/parse-jobs/{job['job_id']}/artifacts/{thumbnail['artifact_id']}")

    assert page_response.status_code == 200
    assert page_response.headers["content-type"] == "image/png"
    assert thumb_response.status_code == 200
    assert thumb_response.headers["content-type"] == "image/jpeg"

    page_path = tmp_path / "page.png"
    thumb_path = tmp_path / "thumb.jpg"
    page_path.write_bytes(page_response.content)
    thumb_path.write_bytes(thumb_response.content)

    with Image.open(page_path) as image:
        assert image.width > 512
        assert image.height > 512
    with Image.open(thumb_path) as image:
        assert max(image.size) == 512
