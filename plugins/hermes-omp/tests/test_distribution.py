from pathlib import Path
import subprocess
import sys
import tarfile

from hermes_omp import __version__


def test_publication_docs_and_ci_exist() -> None:
    root=Path(__file__).parents[1]
    required=["README.md","LICENSE","CHANGELOG.md","docs/INSTALL.md","docs/CONFIGURATION.md","docs/CLI.md","docs/MIGRATION.md","docs/TROUBLESHOOTING.md","docs/SECURITY.md","docs/COMPATIBILITY.md","docs/PUBLISHING.md","examples/config.json",".github/workflows/ci.yml"]
    assert all((root/x).is_file() for x in required)
    all_text="\n".join((root/x).read_text() for x in required if not x.endswith("yml"))
    assert "state.db" in all_text and "hermes send" in all_text and "0.3.0rc1" in all_text
    assert "TELEGRAM_BOT_TOKEN" not in all_text and "/Users/admin" not in all_text


def test_release_version_is_consistent_across_public_surfaces() -> None:
    root = Path(__file__).parents[1]
    assert __version__ == "0.3.0rc1"
    assert 'version = "0.3.0rc1"' in (root / "pyproject.toml").read_text()
    assert "version: 0.3.0rc1" in (root / "plugin" / "plugin.yaml").read_text()
    assert "version: 0.3.0rc1" in (root / "skills" / "omp-service" / "SKILL.md").read_text()


def test_vendored_package_matches_installable_source() -> None:
    root = Path(__file__).parents[1]
    source = root / "src" / "hermes_omp"
    vendored = root / "plugin" / "hermes_omp"
    source_files = {path.name for path in source.glob("*.py")}
    vendored_files = {path.name for path in vendored.glob("*.py")}

    assert vendored_files == source_files
    for name in sorted(source_files):
        assert (vendored / name).read_bytes() == (source / name).read_bytes(), name


def test_developer_install_bootstraps_a_pep_660_capable_pip() -> None:
    root=Path(__file__).parents[1]
    readme=(root/"README.md").read_text()
    workflow=(root/".github/workflows/ci.yml").read_text()
    upgrade="python -m pip install --upgrade 'pip==25.2'"
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


def test_source_archive_contains_self_contained_plugin(tmp_path: Path) -> None:
    root=Path(__file__).parents[1]
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(tmp_path)],
        check=True,
        cwd=root,
    )
    archives=list(tmp_path.glob("hermes_omp-*.tar.gz"))
    assert len(archives)==1
    assert archives[0].name == "hermes_omp-0.3.0rc1.tar.gz"
    with tarfile.open(archives[0]) as archive:
        names=archive.getnames()

    forbidden={"__pycache__", ".pytest_cache", "dist", "build", "artifacts"}
    offending=[
        name for name in names
        if any(component.startswith(".venv") or component in forbidden for component in Path(name).parts)
    ]
    assert offending==[]

    required=[
        "/src/hermes_omp/__init__.py",
        "/plugin/plugin.yaml",
        "/plugin/__init__.py",
        "/plugin/desktop/plugin.js",
        "/plugin/dashboard/plugin_api.py",
        "/skills/omp-service/SKILL.md",
        "/docs/INSTALL.md",
        "/tests/test_plugin.py",
        "/examples/config.json",
        "/README.md",
        "/CHANGELOG.md",
        "/LICENSE",
        "/pyproject.toml",
    ]
    assert all(any(name.endswith(suffix) for name in names) for suffix in required)
