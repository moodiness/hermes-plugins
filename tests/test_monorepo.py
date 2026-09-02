from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "hermes-omp"


def test_catalog_describes_each_discovered_plugin() -> None:
    catalog = json.loads((ROOT / "plugins.json").read_text())
    discovered = sorted(p.name for p in (ROOT / "plugins").iterdir() if (p / "pyproject.toml").is_file())
    assert sorted(item["id"] for item in catalog["plugins"]) == discovered
    item = catalog["plugins"][0]
    assert item["version"] == "0.3.0rc1"
    assert item["path"] == "plugins/hermes-omp"
    assert item["plugin_path"] == "plugins/hermes-omp/plugin"
    assert "source_repository" not in item
    assert item["source_commit"] == "625a7b015d3bd87c6eb4ed2a2e55ed0819a1a61a"
    assert item["autonomous"] is True


def test_plugin_metadata_is_nested_root_relative() -> None:
    pyproject = (PLUGIN / "pyproject.toml").read_text()
    readme = (PLUGIN / "README.md").read_text()
    workflow = (PLUGIN / ".github" / "workflows" / "ci.yml").read_text()
    assert 'plugin-path = "plugin"' in pyproject
    assert 'package-root = "."' in pyproject
    assert "plugins/hermes-omp/plugin" in readme
    assert "PLUGIN_ROOT=plugins/hermes-omp" not in workflow


def test_shared_list_script_discovers_nested_plugin_from_any_cwd(tmp_path: Path) -> None:
    result = subprocess.run([str(ROOT / "scripts" / "plugins"), "list"], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["hermes-omp\tplugins/hermes-omp"]


def test_shared_script_uses_an_isolated_plugin_verification_environment() -> None:
    script = (ROOT / "scripts" / "plugins").read_text()
    assert ".venv-verify" in script
    assert "pip>=21.3" in script
    assert ".[dev]" in script
    assert "python -m pytest" not in script
    assert "rm -rf dist build" not in script


def test_shared_doctor_uses_a_temporary_hermes_home() -> None:
    script = (ROOT / "scripts" / "plugins").read_text()
    assert 'doctor_home="$(mktemp -d ' in script
    assert 'HERMES_HOME="$doctor_home" hermes plugins doctor' in script


def test_shared_build_refreshes_and_checks_release_checksums() -> None:
    script = (ROOT / "scripts" / "plugins").read_text()
    assert "SHA256SUMS" in script
    assert '(cd "$dir/dist" && shasum -a 256 -c "SHA256SUMS")' in script
    assert '(cd "$dir/dist" && shasum -a 256 "$relative")' in script


def test_repository_policy_files_exist() -> None:
    for relative in ["README.md", "LICENSE", "CONTRIBUTING.md", "RELEASING.md", ".github/workflows/ci.yml"]:
        assert (ROOT / relative).is_file(), relative
