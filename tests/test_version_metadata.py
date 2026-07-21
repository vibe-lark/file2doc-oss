from file2doc.version import skill_version_payload, with_version_metadata


def test_current_skill_version_needs_no_update() -> None:
    payload = skill_version_payload("0.1.23")

    assert payload["service_version"] == "0.1.23"
    assert payload["skill"]["status"] == "current"
    assert payload["skill"]["update_available"] is False
    assert payload["skill"]["update_required"] is False
    assert payload["notices"] == []


def test_compatible_old_skill_gets_non_blocking_update_notice() -> None:
    payload = skill_version_payload("0.1.22")

    assert payload["skill"]["status"] == "update_available"
    assert payload["skill"]["update_available"] is True
    assert payload["skill"]["update_required"] is False
    assert payload["notices"][0]["code"] == "skill_update_available"
    assert payload["notices"][0]["level"] == "info"


def test_incompatible_or_invalid_skill_requires_update() -> None:
    incompatible = skill_version_payload("0.1.17")
    invalid = skill_version_payload("legacy")

    assert incompatible["skill"]["status"] == "update_required"
    assert incompatible["skill"]["update_required"] is True
    assert incompatible["notices"][0]["level"] == "warning"
    assert invalid["skill"]["status"] == "invalid"
    assert invalid["skill"]["update_required"] is True


def test_missing_skill_version_exposes_migration_notice() -> None:
    payload = skill_version_payload()

    assert payload["skill"]["status"] == "unknown"
    assert payload["skill"]["installed_version"] is None
    assert payload["skill"]["update_available"] is None
    assert payload["notices"][0]["code"] == "skill_version_unknown"


def test_version_metadata_decorates_response_without_mutating_input() -> None:
    source = {"job_id": "job-1", "status": "queued"}

    decorated = with_version_metadata(source, "0.1.22")

    assert source == {"job_id": "job-1", "status": "queued"}
    assert decorated["job_id"] == "job-1"
    assert decorated["skill"]["latest_version"] == "0.1.23"
