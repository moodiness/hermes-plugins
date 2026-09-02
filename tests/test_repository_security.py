from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_USE = re.compile(r"^\s*-?\s*uses:\s*[^#\s]+@([0-9a-f]{40})(?:\s+#\s+.+)?$", re.MULTILINE)
ANY_USE = re.compile(r"^\s*-?\s*uses:\s*", re.MULTILINE)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if True in data and "on" not in data:  # PyYAML follows YAML 1.1 for the key `on`.
        data["on"] = data.pop(True)
    return data


def test_security_policy_and_codeowners_exist() -> None:
    security = (ROOT / "SECURITY.md").read_text()
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text()
    assert "Privately reporting a security vulnerability" in security
    assert "GitHub private vulnerability reporting" in security
    assert "Supported versions" in security
    assert "Coordinated disclosure" in security
    assert "* @moodiness" in codeowners
    assert "/.github/ @moodiness" in codeowners


def test_dependabot_covers_plugin_python_and_all_action_manifests() -> None:
    config = load_yaml(ROOT / ".github" / "dependabot.yml")
    updates = config["updates"]
    configured = {(entry["package-ecosystem"], entry["directory"]) for entry in updates}
    assert ("pip", "/plugins/hermes-omp") in configured
    assert ("github-actions", "/") in configured
    assert ("github-actions", "/plugins/hermes-omp") in configured
    for entry in updates:
        assert entry["schedule"]["interval"] == "weekly"
        assert entry["open-pull-requests-limit"] > 0


def test_required_workflows_are_present_and_hardened() -> None:
    required = {"ci.yml", "codeql.yml", "dependency-review.yml"}
    assert required <= {path.name for path in WORKFLOWS.glob("*.yml")}
    for path in [*WORKFLOWS.glob("*.yml"), ROOT / "plugins/hermes-omp/.github/workflows/ci.yml"]:
        text = path.read_text()
        data = load_yaml(path)
        assert data.get("permissions") == {"contents": "read"}, path
        assert "concurrency" in data, path
        assert all("timeout-minutes" in job for job in data["jobs"].values()), path
        assert len(SHA_USE.findall(text)) == len(ANY_USE.findall(text)), path


def test_codeql_scans_python_and_javascript_without_manual_build() -> None:
    workflow = load_yaml(WORKFLOWS / "codeql.yml")
    matrix = workflow["jobs"]["analyze"]["strategy"]["matrix"]
    assert matrix["language"] == ["python", "javascript-typescript"]
    assert "build-mode" not in matrix
    assert workflow["jobs"]["analyze"]["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }


def test_dependency_review_is_pull_request_only_and_least_privilege() -> None:
    workflow = load_yaml(WORKFLOWS / "dependency-review.yml")
    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["dependency-review"]["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }


def test_scorecard_is_deliberately_not_enabled_for_private_repository() -> None:
    assert not (WORKFLOWS / "scorecard.yml").exists()
    decision = (ROOT / ".github" / "SECURITY_TOOLS.md").read_text()
    assert "Scorecard" in decision
    assert "private repository" in decision


def test_repository_has_no_internal_ai_workflow_provenance() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout.splitlines()
    assert not any(path.startswith("docs/superpowers/") for path in tracked)

    prohibited = re.compile(
        r"(?i)(generated|authored|reviewed|implemented|written|planned)\s+(?:by|with)\s+"
        r"(?:claude|codex|hermes(?: agent)?|superpowers)|agentic[- ]worker instructions"
    )
    text_files = [
        path for path in tracked
        if Path(path).suffix.lower() in {".md", ".txt", ".yml", ".yaml", ".toml"}
    ]
    findings = []
    for relative in text_files:
        text = (ROOT / relative).read_text(errors="replace")
        if prohibited.search(text):
            findings.append(relative)
    assert not findings, findings
