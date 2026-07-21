from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_api_docs_preserve_result_retrieval_contract() -> None:
    api_examples = (ROOT / "docs" / "api-examples.md").read_text(encoding="utf-8")
    skill_guide = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    combined = f"{api_examples}\n{skill_guide}"

    assert "https://file2doc.solutionsuite.cn" in api_examples
    assert "file2doc.prd.solutionsuite.cn" not in combined
    assert "GET /parse-jobs/{job_id}/result" in combined
    assert "content.artifact_id" in combined
    assert "/parse-jobs/{job_id}/artifacts/{artifact_id}" in combined
    assert "artifacts/media_index" in combined
    assert '"content": {' in api_examples
    assert '"artifact_id": "art_content"' in api_examples
    assert '"artifacts": [' in api_examples
    assert '"artifacts": {' not in api_examples
    assert "empty_result" in combined
    assert "npx skills add vibe-lark/file2doc-skill --skill file2doc-http" in skill_guide
    assert "npx skills update file2doc-http" in skill_guide
    assert 'version: "0.1.23"' in skill_guide
    assert "/skills/file2doc-http/version.json" in skill_guide
    assert "X-File2Doc-Skill-Version" in skill_guide
    assert "vibe-lark/file2doc-skill" in skill_guide
    assert "## Final Document" in skill_guide
    assert "### Writing and Structure" in skill_guide
    assert "### Visuals" in skill_guide
    assert "### Source Attribution" in skill_guide
    assert "### Delivery Check" in skill_guide
    assert "If the user only asks to parse" in skill_guide
    assert "Do not paste raw parser output unchanged" in skill_guide
    assert "Put each image immediately after" in skill_guide
    assert "each major workflow should include at least one useful source image" in skill_guide
    assert "every source is traceable" in skill_guide
    assert "clickable original URL" in skill_guide
    assert "attach the original file" in skill_guide
    assert "local path is not a traceable source" in skill_guide


def test_agent_api_docs_cover_core_source_modalities() -> None:
    api_examples = (ROOT / "docs" / "api-examples.md").read_text(encoding="utf-8")

    for marker in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "audio/mpeg",
        "video/mp4",
    ):
        assert marker in api_examples
