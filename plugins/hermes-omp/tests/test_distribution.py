from pathlib import Path


def test_publication_docs_and_ci_exist() -> None:
    root=Path(__file__).parents[1]
    required=["README.md","LICENSE","CHANGELOG.md","docs/INSTALL.md","docs/CONFIGURATION.md","docs/CLI.md","docs/MIGRATION.md","docs/TROUBLESHOOTING.md","docs/SECURITY.md","docs/COMPATIBILITY.md","docs/PUBLISHING.md","examples/config.json",".github/workflows/ci.yml"]
    assert all((root/x).is_file() for x in required)
    all_text="\n".join((root/x).read_text() for x in required if not x.endswith("yml"))
    assert "state.db" in all_text and "hermes send" in all_text and "0.1.0rc1" in all_text
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
    assert editable in workflow
    assert "PYTHONPATH" not in workflow


def test_no_prohibited_runtime_coupling() -> None:
    root=Path(__file__).parents[1]
    production="\n".join(p.read_text() for p in (root/"src").rglob("*.py"))
    assert "state.db" not in production
    assert "api.telegram.org" not in production
    assert "TELEGRAM_BOT_TOKEN" not in production
    assert "shell=True" not in production
