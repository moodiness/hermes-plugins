from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import hermes_omp.cli as cli
from hermes_omp.cli import build_parser, main
from hermes_omp.core import Paths, SessionStore


def invoke(tmp_path: Path, capsys, *args: str) -> tuple[int, str]:
    os.environ["HERMES_HOME"] = str(tmp_path)
    rc = main(list(args))
    return rc, capsys.readouterr().out


def test_cli_exposes_required_commands() -> None:
    parser = build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert {"doctor","create","adopt","list","status","send","logs","events","retry","export","import","update","stop","restart","remove","config","completion","run","inbound"} <= set(action.choices)


def test_create_list_status_send_remove_without_activation(tmp_path: Path, capsys) -> None:
    rc, out = invoke(tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "go", "--platform", "telegram", "--chat", "42", "--topic", "7", "--omp-path", "/bin/true", "--no-install", "--json")
    assert rc == 0 and json.loads(out)["name"] == "demo"
    rc, out = invoke(tmp_path, capsys, "list", "--json")
    assert json.loads(out)["sessions"][0]["name"] == "demo"
    rc, out = invoke(tmp_path, capsys, "status", "demo", "--json")
    assert json.loads(out)["status"] == "created"
    rc, out = invoke(tmp_path, capsys, "send", "demo", "follow up", "--json")
    assert json.loads(out)["queued"] is True
    rc, out = invoke(tmp_path, capsys, "remove", "demo", "--no-service", "--json")
    assert rc == 0
    rc, out = invoke(tmp_path, capsys, "list", "--json")
    assert json.loads(out)["sessions"] == []


def test_duplicate_omp_session_id_is_rejected(tmp_path: Path, capsys) -> None:
    common=("--cwd",str(tmp_path),"--model","m","--mission","x","--resume","sid","--omp-path","/bin/true","--no-install")
    assert invoke(tmp_path,capsys,"create","one",*common)[0] == 0
    rc, out = invoke(tmp_path,capsys,"create","two",*common,"--json")
    assert rc == cli.EXIT_VALIDATION and "already owned" in json.loads(out)["error"]["message"]


def test_same_name_create_conflicts_without_overwriting_or_deleting(
    tmp_path: Path, capsys
) -> None:
    first = (
        "create", "demo", "--cwd", str(tmp_path), "--model", "first",
        "--mission", "original", "--omp-path", "/bin/true", "--no-install",
    )
    assert invoke(tmp_path, capsys, *first)[0] == 0
    paths = Paths.discover()
    session_path = paths.sessions / "demo.json"
    omp_path = paths.run / "demo.omp-path"
    original_session = session_path.read_bytes()
    original_omp_path = omp_path.read_bytes()

    rc, out = invoke(
        tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path),
        "--model", "second", "--mission", "replacement", "--resume", "other-id",
        "--omp-path", "/different/omp", "--no-install", "--json",
    )

    assert rc == cli.EXIT_CONFLICT
    assert json.loads(out)["error"]["code"] == "conflict"
    assert session_path.read_bytes() == original_session
    assert omp_path.read_bytes() == original_omp_path


def test_preinstall_write_failure_never_removes_service(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[str] = []

    class Backend:
        def definition_path(self, name):
            return tmp_path / f"{name}.service"

        def remove(self, name):
            removed.append(name)

    monkeypatch.setattr(cli, "backend_for", lambda **kwargs: Backend())
    original_atomic_write = cli.atomic_write

    def fail_omp_path(path, data, mode=0o600):
        if path.name == "demo.omp-path":
            raise OSError("injected omp-path failure")
        return original_atomic_write(path, data, mode)

    monkeypatch.setattr(cli, "atomic_write", fail_omp_path)

    with pytest.raises(OSError, match="injected omp-path failure"):
        invoke(
            tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path),
            "--model", "m", "--mission", "x", "--omp-path", "/bin/true",
        )

    paths = Paths.discover()
    assert removed == []
    assert not (paths.sessions / "demo.json").exists()
    assert not (paths.run / "demo.omp-path").exists()


def test_update_waits_for_replacement_and_merges_new_identity(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke(
        tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path),
        "--model", "old", "--mission", "original", "--omp-path", "/bin/true",
        "--no-install",
    )[0] == 0
    paths = Paths.discover()
    store = SessionStore(paths)
    replacement = cli.Session.new(
        name="demo", cwd=str(tmp_path), model="replacement", mission="replacement mission"
    )
    started = threading.Event()
    finished = threading.Event()
    results: list[int] = []

    def update() -> None:
        started.set()
        results.append(cli.main([
            "update", "demo", "--model", "updated", "--no-install", "--json",
        ]))
        finished.set()

    with store.transaction():
        worker = threading.Thread(target=update)
        worker.start()
        assert started.wait(1) and not finished.wait(0.1)
        store.replace(replacement)
    worker.join(2)

    current = store.load("demo")
    assert results == [0]
    assert current.id == replacement.id
    assert current.model == "updated"
    assert current.mission == "replacement mission"


def test_remove_waits_for_replacement_and_does_not_resurrect_state(
    tmp_path: Path, capsys
) -> None:
    assert invoke(
        tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path),
        "--model", "old", "--mission", "original", "--omp-path", "/bin/true",
        "--no-install",
    )[0] == 0
    paths = Paths.discover()
    store = SessionStore(paths)
    replacement = cli.Session.new(
        name="demo", cwd=str(tmp_path), model="replacement", mission="replacement mission"
    )
    started = threading.Event()
    finished = threading.Event()
    results: list[int] = []

    def remove() -> None:
        started.set()
        results.append(cli.main(["remove", "demo", "--no-service", "--json"]))
        finished.set()

    with store.transaction():
        worker = threading.Thread(target=remove)
        worker.start()
        assert started.wait(1) and not finished.wait(0.1)
        store.replace(replacement)
    worker.join(2)

    assert results == [0]
    assert not (paths.sessions / "demo.json").exists()
    assert not (paths.run / "demo.omp-path").exists()


def test_adopt_uses_explicit_inspection_file_not_process_mutation(tmp_path: Path, capsys) -> None:
    inspection=tmp_path/"inspection.json"; inspection.write_text(json.dumps({"argv":["omp","--resume","sid","--model","m"],"cwd":str(tmp_path)}))
    rc,out=invoke(tmp_path,capsys,"adopt","adopted","--inspection",str(inspection),"--mission","continue","--omp-path","/bin/true","--no-install","--json")
    assert rc==0 and json.loads(out)["omp_session_id"]=="sid"


def test_inbound_submits_public_envelope(tmp_path: Path, capsys) -> None:
    invoke(tmp_path,capsys,"create","demo","--cwd",str(tmp_path),"--model","m","--mission","x","--platform","p","--chat","c","--topic","t","--omp-path","/bin/true","--no-install")
    rc,out=invoke(tmp_path,capsys,"inbound","demo","--event-id","evt","--question-id","q","--platform","p","--chat","c","--topic","t","--user","u","--answer","1","--json")
    assert rc==0 and json.loads(out)["queued"] is True and "accepted" not in json.loads(out)


def test_doctor_is_structured_and_never_reads_secrets(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME",str(tmp_path)); monkeypatch.setenv("HERMES_OMP_BINARY","/bin/true"); monkeypatch.setenv("HERMES_OMP_HERMES","/bin/true")
    rc,out=invoke(tmp_path,capsys,"doctor","--json")
    report=json.loads(out)
    assert rc==0 and report["ok"] is True and report["state_db_used"] is False and report["telegram_api_used"] is False


@pytest.mark.parametrize("command", ["create", "adopt"])
def test_create_and_adopt_roll_back_state_when_service_install_fails(tmp_path: Path, capsys, monkeypatch, command: str) -> None:
    class BrokenBackend:
        def definition_path(self, name): return tmp_path / f"{name}.service"
        def install(self,*args,**kwargs): raise subprocess.CalledProcessError(1,["install"])
        def remove(self,name): pass
    monkeypatch.setattr(cli,"backend_for",lambda **kwargs: BrokenBackend())
    args=["create","demo","--cwd",str(tmp_path),"--model","m","--mission","x","--omp-path","/bin/true"]
    if command == "adopt":
        inspection=tmp_path/"inspection.json"; inspection.write_text(json.dumps({"argv":["omp","--resume","sid","--model","m"],"cwd":str(tmp_path)}))
        args=["adopt","demo","--inspection",str(inspection),"--mission","x","--omp-path","/bin/true"]
    with pytest.raises(subprocess.CalledProcessError): invoke(tmp_path,capsys,*args)
    paths=Paths.discover()
    assert not (paths.sessions/"demo.json").exists() and not (paths.run/"demo.omp-path").exists()


def test_remove_refuses_live_owner_and_preserves_state(tmp_path: Path, capsys) -> None:
    invoke(tmp_path,capsys,"create","demo","--cwd",str(tmp_path),"--model","m","--mission","x","--omp-path","/bin/true","--no-install")
    paths=Paths.discover(); lock=paths.run/"demo.owner"; lock.write_text(json.dumps({"pid":os.getpid(),"session_id":SessionStore(paths).load("demo").id,"token":"live"}))
    rc, out = invoke(tmp_path,capsys,"remove","demo","--no-service","--json")
    assert rc == cli.EXIT_CONFLICT and "still running" in json.loads(out)["error"]["message"]
    assert lock.exists() and (paths.sessions/"demo.json").exists()


def _finite_owned_group() -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"], start_new_session=True, text=True)



def _orphan_marker(child: subprocess.Popen[str]) -> dict[str, int]:
    key = "orphaned_pid" if os.name == "nt" else "orphaned_pgid"
    return {key: child.pid}

def test_remove_refuses_live_orphan_identity_then_recovers(tmp_path: Path, capsys) -> None:
    invoke(tmp_path,capsys,"create","demo","--cwd",str(tmp_path),"--model","m","--mission","x","--omp-path","/bin/true","--no-install")
    paths=Paths.discover(); lock=paths.run/"demo.owner"; session=SessionStore(paths).load("demo")
    child=_finite_owned_group()
    lock.write_text(json.dumps({"pid":99999999,"session_id":session.id,"token":"orphan",**_orphan_marker(child)}))
    try:
        rc, out = invoke(tmp_path,capsys,"remove","demo","--no-service","--json")
        refused = rc == cli.EXIT_CONFLICT and "still running" in json.loads(out)["error"]["message"]
        preserved = lock.exists() and (paths.sessions/"demo.json").exists()
    finally:
        child.wait(timeout=3)
    rc, _ = invoke(tmp_path,capsys,"remove","demo","--no-service","--json")
    assert refused and preserved
    assert rc == 0


def test_doctor_fix_retains_live_orphan_identity_then_removes_stale_marker(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_OMP_BINARY","/bin/true"); monkeypatch.setenv("HERMES_OMP_HERMES","/bin/true")
    paths=Paths(tmp_path/"omp"); paths.ensure(); lock=paths.run/"demo.owner"
    child=_finite_owned_group()
    lock.write_text(json.dumps({"pid":99999999,"session_id":"session","token":"orphan",**_orphan_marker(child)}))
    try:
        rc, _ = invoke(tmp_path,capsys,"doctor","--fix","--json")
        retained = rc == 0 and lock.exists()
    finally:
        child.wait(timeout=3)
    rc, _ = invoke(tmp_path,capsys,"doctor","--fix","--json")
    assert retained
    assert rc == 0 and not lock.exists()


def test_orphan_marker_uses_platform_process_identity(monkeypatch) -> None:
    class Child:
        pid = 123

    monkeypatch.setattr(cli.os, "name", "nt")
    assert _orphan_marker(Child()) == {"orphaned_pid": 123}
