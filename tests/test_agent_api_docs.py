from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_api_docs_preserve_result_retrieval_contract() -> None:
    api_examples = (ROOT / "docs" / "api-examples.md").read_text(encoding="utf-8")
    skill_guide = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    combined = f"{api_examples}\n{skill_guide}"

    assert "https://file2doc.example.com" in api_examples
    assert "GET /parse-jobs/{job_id}/result" in combined
    assert "content.artifact_id" in combined
    assert "/parse-jobs/{job_id}/artifacts/{artifact_id}" in combined
    assert "artifacts/media_index" in combined
    assert '"content": {' in api_examples
    assert '"artifact_id": "art_content"' in api_examples
    assert '"artifacts": [' in api_examples
    assert '"artifacts": {' not in api_examples
    assert "empty_parse_result" in combined


def test_agent_api_docs_cover_core_source_modalities() -> None:
    api_examples = (ROOT / "docs" / "api-examples.md").read_text(encoding="utf-8")

    for marker in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "audio/mpeg",
        "video/mp4",
    ):
        assert marker in api_examples
