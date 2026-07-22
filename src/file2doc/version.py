from __future__ import annotations

import re
from typing import Any


SERVICE_VERSION = "0.1.25"
SKILL_NAME = "file2doc-http"
SKILL_VERSION = "0.1.25"
MINIMUM_COMPATIBLE_SKILL_VERSION = "0.1.18"
SKILL_VERSION_URL = (
    "https://file2doc.solutionsuite.cn/skills/file2doc-http/version.json"
)
GATEWAY_UPDATE_COMMAND = (
    "curl -fsSL https://file2doc.solutionsuite.cn/skills/file2doc-http/install.sh | sh"
)
GITHUB_UPDATE_COMMAND = "npx skills update file2doc-http"
GITHUB_INSTALL_COMMAND = (
    "npx skills add vibe-lark/file2doc-skill --skill file2doc-http"
)

_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_PATTERN.fullmatch(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def skill_version_payload(installed_version: str | None = None) -> dict[str, Any]:
    latest = _parse_version(SKILL_VERSION)
    minimum = _parse_version(MINIMUM_COMPATIBLE_SKILL_VERSION)
    installed = _parse_version(installed_version) if installed_version else None

    notices: list[dict[str, str]] = []
    if installed_version is None:
        status = "unknown"
        update_available: bool | None = None
        update_required = False
        notices.append(
            {
                "code": "skill_version_unknown",
                "level": "info",
                "message": "Skill version was not provided; check for a File2Doc Skill update.",
            }
        )
    elif installed is None:
        status = "invalid"
        update_available = True
        update_required = True
        notices.append(
            {
                "code": "skill_update_required",
                "level": "warning",
                "message": "Installed Skill version is invalid; update before the next task.",
            }
        )
    elif installed < minimum:
        status = "update_required"
        update_available = True
        update_required = True
        notices.append(
            {
                "code": "skill_update_required",
                "level": "warning",
                "message": "Installed Skill is no longer compatible; update before the next task.",
            }
        )
    elif installed < latest:
        status = "update_available"
        update_available = True
        update_required = False
        notices.append(
            {
                "code": "skill_update_available",
                "level": "info",
                "message": "A newer File2Doc Skill is available.",
            }
        )
    elif installed > latest:
        status = "newer"
        update_available = False
        update_required = False
    else:
        status = "current"
        update_available = False
        update_required = False

    return {
        "service_version": SERVICE_VERSION,
        "skill": {
            "name": SKILL_NAME,
            "installed_version": installed_version,
            "latest_version": SKILL_VERSION,
            "minimum_compatible_version": MINIMUM_COMPATIBLE_SKILL_VERSION,
            "status": status,
            "update_available": update_available,
            "update_required": update_required,
            "version_url": SKILL_VERSION_URL,
            "gateway_update_command": GATEWAY_UPDATE_COMMAND,
            "github_update_command": GITHUB_UPDATE_COMMAND,
            "github_install_command": GITHUB_INSTALL_COMMAND,
        },
        "notices": notices,
    }


def with_version_metadata(
    payload: dict[str, Any],
    installed_version: str | None = None,
) -> dict[str, Any]:
    return payload | skill_version_payload(installed_version)
