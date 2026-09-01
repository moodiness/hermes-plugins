from pathlib import Path
import tarfile


def test_publication_docs_and_ci_exist() -> None:
    root=Path(__file__).parents[1]
    required=["README.md","LICENSE","CHANGELOG.md","docs/INSTALL.md","docs/CONFIGURATION.md","docs/CLI.md","docs/MIGRATION.md","docs/TROUBLESHOOTING.md","docs/SECURITY.md","docs/COMPATIBILITY.md","docs/PUBLISHING.md","examples/config.json",".github/workflows/ci.yml"]
    assert all((root/x).is_file() for x in required)
    all_text="\n".join((root/x).read_text() for x in required if not x.endswith("yml"))
    assert "state.db" in all_text and "hermes send" in all_text and "0.2.0rc1" in all_text
    assert "TELEGRAM_BOT_TOKEN" not in all_text and "/Users/admin" not in all_text


def test_developer_install_bootstraps_a_pep_660_capable_pip() -> None:
    root=Path(__file__).parents[1]
    readme=(root/"README.md").read_text()
    workflow=(root/".github/workflows/ci.yml").read_text()
    upgrade="python -m pip install --upgrade 'pip>=21.3'"
    editable="python -m pip install -e '.[dev]'"
    assert upgrade in readme
    assert editable in readme
    assert readme.index(upgrade) < readme.index(editable)
    assert upgrade in workflow
    assert "python -m pip install -e '.[dev]'" in workflow
    assert "PLUGIN_ROOT" not in workflow
    assert "PYTHONPATH" not in workflow


def test_no_prohibited_runtime_coupling() -> None:
    root=Path(__file__).parents[1]
    production="\n".join(p.read_text() for p in (root/"src").rglob("*.py"))
    assert "state.db" not in production
    assert "api.telegram.org" not in production
    assert "TELEGRAM_BOT_TOKEN" not in production
    assert "shell=True" not in production


def test_monorepo_migration_metadata_is_plugin_relative() -> None:
    root=Path(__file__).parents[1]
    pyproject=(root/"pyproject.toml").read_text()
    readme=(root/"README.md").read_text()
    workflow=(root/".github/workflows/ci.yml").read_text()
    assert 'plugin-path = "plugin"' in pyproject
    assert "plugins/hermes-omp/plugin" in readme
    assert "PLUGIN_ROOT=plugins/hermes-omp" not in workflow
    assert "/Users/" not in pyproject + workflow


def test_source_archive_contains_self_contained_plugin() -> None:
    root=Path(__file__).parents[1]
    archives=sorted((root/"dist").glob("hermes_omp-*.tar.gz"))
    if not archives:
        return
    with tarfile.open(archives[-1]) as archive:
        names=archive.getnames()
    assert any(name.endswith("/plugin/plugin.yaml") for name in names)
    assert any(name.endswith("/plugin/__init__.py") for name in names)
