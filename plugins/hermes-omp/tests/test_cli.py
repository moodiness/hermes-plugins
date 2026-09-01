from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from hermes_omp.cli import build_parser, main


def invoke(tmp_path: Path, capsys, *args: str) -> tuple[int, str]:
    os.environ["HERMES_HOME"] = str(tmp_path)
    rc = main(list(args))
    return rc, capsys.readouterr().out


def test_cli_exposes_required_commands() -> None:
    parser = build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(action.choices) == {"doctor","create","adopt","list","status","send","logs","stop","restart","remove","run","inbound"}


def test_create_list_status_send_remove_without_activation(tmp_path: Path, capsys) -> None:
    rc, out = invoke(tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "go", "--platform", "telegram", "--chat", "42", "--topic", "7", "--omp-path", "/bin/true", "--no-install")
    assert rc == 0 and json.loads(out)["name"] == "demo"
    rc, out = invoke(tmp_path, capsys, "list", "--json")
    assert json.loads(out)[0]["name"] == "demo"
    rc, out = invoke(tmp_path, capsys, "status", "demo", "--json")
    assert json.loads(out)["status"] == "created"
    rc, out = invoke(tmp_path, capsys, "send", "demo", "follow up")
    assert json.loads(out)["queued"] is True
    rc, out = invoke(tmp_path, capsys, "remove", "demo", "--no-service")
    assert rc == 0
    rc, out = invoke(tmp_path, capsys, "list", "--json")
    assert json.loads(out) == []


def test_duplicate_omp_session_id_is_rejected(tmp_path: Path, capsys) -> None:
    common=("--cwd",str(tmp_path),"--model","m","--mission","x","--resume","sid","--omp-path","/bin/true","--no-install")
    assert invoke(tmp_path,capsys,"create","one",*common)[0] == 0
    with pytest.raises(ValueError,match="already owned"):
        invoke(tmp_path,capsys,"create","two",*common)


def test_adopt_uses_explicit_inspection_file_not_process_mutation(tmp_path: Path, capsys) -> None:
    inspection=tmp_path/"inspection.json"; inspection.write_text(json.dumps({"argv":["omp","--resume","sid","--model","m"],"cwd":str(tmp_path)}))
    rc,out=invoke(tmp_path,capsys,"adopt","adopted","--inspection",str(inspection),"--mission","continue","--omp-path","/bin/true","--no-install")
    assert rc==0 and json.loads(out)["omp_session_id"]=="sid"


def test_inbound_submits_public_envelope(tmp_path: Path, capsys) -> None:
    invoke(tmp_path,capsys,"create","demo","--cwd",str(tmp_path),"--model","m","--mission","x","--platform","p","--chat","c","--topic","t","--omp-path","/bin/true","--no-install")
    rc,out=invoke(tmp_path,capsys,"inbound","demo","--event-id","evt","--question-id","q","--platform","p","--chat","c","--topic","t","--user","u","--answer","1")
    assert rc==0 and json.loads(out)["accepted"] is True


def test_doctor_is_structured_and_never_reads_secrets(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME",str(tmp_path)); monkeypatch.setenv("HERMES_OMP_BINARY","/bin/true"); monkeypatch.setenv("HERMES_OMP_HERMES","/bin/true")
    rc,out=invoke(tmp_path,capsys,"doctor","--json")
    report=json.loads(out)
    assert rc==0 and report["ok"] is True and report["state_db_used"] is False and report["telegram_api_used"] is False
