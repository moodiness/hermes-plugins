from __future__ import annotations

import argparse
import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

def load_plugin():
    root=Path(__file__).parents[1]/"plugin"
    import sys
    for name in list(sys.modules):
        if name == "hermes_omp_plugin" or name.startswith("hermes_omp_plugin."):
            sys.modules.pop(name)
    spec=importlib.util.spec_from_file_location("hermes_omp_plugin",root/"__init__.py",submodule_search_locations=[str(root)])
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); return module


def test_plugin_registers_only_public_cli_command() -> None:
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    load_plugin().register(Context())
    assert len(calls)==1 and calls[0]["name"]=="omp"
    assert callable(calls[0]["setup_fn"]) and callable(calls[0]["handler_fn"])


def test_plugin_cli_setup_has_all_required_commands() -> None:
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    load_plugin().register(Context())
    parser=argparse.ArgumentParser(); calls[0]["setup_fn"](parser)
    help_text = parser.format_help()
    for command in {"doctor","create","adopt","list","status","send","logs","events","retry","export","import","update","config","completion","stop","restart","remove","migrate-legacy","watch","diagnose","clone"}:
        assert command in help_text


def test_copied_plugin_is_self_contained_without_installed_distribution(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parents[1] / "plugin"
    copied = tmp_path / "omp"
    shutil.copytree(source, copied)
    assert (copied / "hermes_omp" / "cli.py").is_file()
    script = """
import argparse
import importlib.util
import sys

plugin_dir = sys.argv[1]
spec = importlib.util.spec_from_file_location(
    "isolated_omp_plugin",
    plugin_dir + "/__init__.py",
    submodule_search_locations=[plugin_dir],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

calls = []
class Context:
    def register_cli_command(self, **kwargs):
        calls.append(kwargs)

module.register(Context())
parser = argparse.ArgumentParser()
calls[0]["setup_fn"](parser)
assert "doctor" in parser.format_help()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(copied)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_registration_declares_authoritative_cli_metadata(caplog) -> None:
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    with caplog.at_level(logging.DEBUG): load_plugin().register(Context())
    assert len(calls)==1
    assert set(calls[0])=={"name", "help", "setup_fn", "handler_fn", "description"}
    assert calls[0]["name"]=="omp"
    assert calls[0]["help"]=="Durable Oh My Pi session supervision"
    assert calls[0]["description"]=="Create and supervise durable OMP RPC sessions."
    assert callable(calls[0]["setup_fn"])
    assert callable(calls[0]["handler_fn"])
    assert "registered `hermes omp`" in caplog.text


def test_plugin_dispatch_preserves_zero_and_false_values(monkeypatch) -> None:
    received=[]
    import hermes_omp.cli
    monkeypatch.setattr(
        hermes_omp.cli,
        "dispatch_namespace",
        lambda args: received.append(args) or 0,
    )
    plugin=load_plugin()
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    plugin.register(Context())
    parser=argparse.ArgumentParser()
    calls[0]["setup_fn"](parser)
    args=parser.parse_args([
        "logs", "demo", "--lines", "0", "--poll-interval", "0",
        "--max-polls", "0", "--json",
    ])

    assert calls[0]["handler_fn"](args)==0
    assert len(received)==1
    assert (received[0].lines, received[0].poll_interval, received[0].max_polls)==(0, 0.0, 0)
    assert received[0].follow is False


def test_plugin_registration_uses_only_public_parser_api(monkeypatch) -> None:
    import hermes_omp.cli

    def private_template_path() -> None:
        raise AssertionError("plugin must not build or inspect a template parser")

    monkeypatch.setattr(hermes_omp.cli, "build_parser", private_template_path)
    plugin=load_plugin()
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    plugin.register(Context())

    class PublicParser:
        def __init__(self) -> None:
            self.parser=argparse.ArgumentParser()

        @property
        def description(self):
            return self.parser.description

        @description.setter
        def description(self, value):
            self.parser.description=value

        def add_subparsers(self, **kwargs):
            return self.parser.add_subparsers(**kwargs)

        def set_defaults(self, **kwargs):
            self.parser.set_defaults(**kwargs)

    parser=PublicParser()
    calls[0]["setup_fn"](parser)
    assert "logs" in parser.parser.format_help()


def test_plugin_duplicate_create_returns_json_conflict(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_omp.cli
    monkeypatch.setattr(hermes_omp.cli, "_version", lambda command: "test")
    plugin=load_plugin()
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    plugin.register(Context())
    parser=argparse.ArgumentParser()
    calls[0]["setup_fn"](parser)
    argv=[
        "create", "demo", "--cwd", str(tmp_path), "--model", "m",
        "--mission", "mission", "--omp-path", "/bin/true", "--no-install",
        "--json",
    ]
    assert calls[0]["handler_fn"](parser.parse_args(argv))==0
    capsys.readouterr()

    result=calls[0]["handler_fn"](parser.parse_args(argv))

    assert result==hermes_omp.cli.EXIT_CONFLICT
    import json
    assert json.loads(capsys.readouterr().out)=={
        "ok": False,
        "error": {"code": "conflict", "message": "session already exists: demo"},
    }
