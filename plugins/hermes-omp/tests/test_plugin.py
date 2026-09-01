from __future__ import annotations

import argparse
import importlib.util
import logging
from pathlib import Path

def load_plugin():
    root=Path(__file__).parents[1]/"plugin"
    spec=importlib.util.spec_from_file_location("hermes_omp_plugin",root/"__init__.py",submodule_search_locations=[str(root)])
    import sys
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
    action=next(a for a in parser._actions if isinstance(a,argparse._SubParsersAction))
    assert {"doctor","create","adopt","list","status","send","logs","events","retry","export","import","update","config","completion","stop","restart","remove"} <= set(action.choices)


def test_manifest_and_registration_follow_official_example_shape(caplog) -> None:
    root=Path(__file__).parents[1]/"plugin"
    manifest=(root/"plugin.yaml").read_text()
    assert "name: omp" in manifest and "hooks: []" in manifest
    assert "provides:\n  cli_commands:\n    - omp" in manifest
    assert "kind:" not in manifest and "provides_tools:" not in manifest
    calls=[]
    class Context:
        def register_cli_command(self,**kwargs): calls.append(kwargs)
    with caplog.at_level(logging.DEBUG): load_plugin().register(Context())
    assert "registered `hermes omp`" in caplog.text


def test_plugin_dispatch_preserves_multi_position_commands(tmp_path, monkeypatch) -> None:
    plugin=load_plugin()
    calls=[]
    import hermes_omp.cli
    monkeypatch.setattr(hermes_omp.cli,"main",lambda argv: calls.append(argv) or 0)
    args=argparse.Namespace(command="export",name="demo",archive=str(tmp_path/"a.json"),json=True,func=None)
    assert plugin.dispatch(args)==0
    assert calls==[["export","demo",str(tmp_path/"a.json"),"--json"]]
