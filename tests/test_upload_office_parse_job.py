from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
from openpyxl import Workbook

from file2doc.app import create_app
from fixtures import sample_file


def test_uploaded_docx_produces_non_empty_markitdown_markdown(tmp_path):
    sample = tmp_path / "office-support.docx"
    document = Document()
    document.add_heading("Quarterly Office Support", level=1)
    document.add_paragraph("DOCX generated sample marker: alpha roadmap.")
    document.save(sample)
    client = TestClient(create_app(storage_root=tmp_path / "storage", auth_enabled=False))

    create_response = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                sample.name,
                sample.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"parser_profile": "agent", "retention": "short"},
    )

    assert create_response.status_code == 201
    created = create_response.json()

    poll_response = client.get(created["poll_url"])
    assert poll_response.status_code == 200
    job = poll_response.json()
    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["percent"] == 100

    manifest_response = client.get(job["result"]["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["source"]["filename"] == sample.name
    assert manifest["source"]["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert manifest["content"]["artifact_id"] == job["result"]["content_artifact_id"]
    assert manifest["parser"]["name"] == "markitdown"
    assert manifest["parser"]["version"]

    content_response = client.get(job["result"]["content_url"])
    assert content_response.status_code == 200
    assert content_response.headers["content-type"].startswith("text/markdown")
    content = content_response.text
    assert "Quarterly Office Support" in content
    assert "DOCX generated sample marker: alpha roadmap." in content


def test_uploaded_xlsx_produces_non_empty_markitdown_markdown(tmp_path):
    sample = tmp_path / "office-support.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Summary"
    worksheet.append(["Metric", "Value"])
    worksheet.append(["XLSX generated sample marker", "beta spreadsheet"])
    workbook.save(sample)
    client = TestClient(create_app(storage_root=tmp_path / "storage", auth_enabled=False))

    create_response = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                sample.name,
                sample.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"parser_profile": "agent", "retention": "short"},
    )

    assert create_response.status_code == 201
    created = create_response.json()

    poll_response = client.get(created["poll_url"])
    assert poll_response.status_code == 200
    job = poll_response.json()
    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["percent"] == 100

    manifest_response = client.get(job["result"]["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["source"]["filename"] == sample.name
    assert manifest["source"]["content_type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert manifest["content"]["artifact_id"] == job["result"]["content_artifact_id"]
    assert manifest["parser"]["name"] == "markitdown"
    assert manifest["parser"]["version"]

    content_response = client.get(job["result"]["content_url"])
    assert content_response.status_code == 200
    assert content_response.headers["content-type"].startswith("text/markdown")
    content = content_response.text
    assert "Summary" in content
    assert "XLSX generated sample marker" in content
    assert "beta spreadsheet" in content


def test_uploaded_docx_with_no_extracted_markdown_fails_visibly(tmp_path):
    sample = tmp_path / "blank-office-support.docx"
    Document().save(sample)
    client = TestClient(create_app(storage_root=tmp_path / "storage", auth_enabled=False))

    created = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                sample.name,
                sample.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    ).json()
    job = client.get(created["poll_url"]).json()

    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert job["error"]["code"] == "empty_parse_result"
    assert job["result"] is None

    manifest_response = client.get(f"/parse-jobs/{created['job_id']}/result")
    assert manifest_response.status_code == 409
    assert manifest_response.json()["detail"]["code"] == "result_not_ready"


def test_uploaded_pptx_produces_non_empty_markitdown_markdown(tmp_path):
    sample = sample_file("1.1.1 基础系列-导读课-大模型技术趋势与企业级 LLMOps 平台价值解读.pptx")
    client = TestClient(create_app(storage_root=tmp_path, auth_enabled=False))

    create_response = client.post(
        "/parse-jobs/upload",
        files={
            "file": (
                sample.name,
                sample.read_bytes(),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
        data={"parser_profile": "agent", "retention": "short"},
    )

    assert create_response.status_code == 201
    created = create_response.json()

    poll_response = client.get(created["poll_url"])
    assert poll_response.status_code == 200
    job = poll_response.json()
    assert job["status"] == "completed"
    assert job["stage"] == "completed"
    assert job["percent"] == 100

    manifest_response = client.get(job["result"]["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["source"]["filename"] == sample.name
    assert manifest["source"]["content_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert manifest["content"]["artifact_id"] == job["result"]["content_artifact_id"]

    content_response = client.get(job["result"]["content_url"])
    assert content_response.status_code == 200
    assert content_response.headers["content-type"].startswith("text/markdown")
    content = content_response.text
    assert "大模型技术趋势与企业级 LLMOps 平台价值解读" in content
    assert "AI - 你的必备「数字生产力」" in content
    assert len(content) > 1000
