from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.runtime import Runtime, build_omp_command, inspect_adoption


def test_build_omp_rpc_command_new_and_resume() -> None:
    session = Session.new(name="x", cwd=".", model="m", mission="go", omp_options=["--thinking", "high"])
    assert build_omp_command(session, "/bin/omp") == ["/bin/omp", "--thinking", "high", "--mode", "rpc", "--model", "m"]
    session.omp_session_id = "sid"
    assert build_omp_command(session, "/bin/omp")[-2:] == ["--resume", "sid"]


def test_adoption_requires_explicit_session_and_safe_argv() -> None:
    info = inspect_adoption(["omp", "--mode", "rpc", "--resume", "abc", "--model", "m"], "/tmp")
    assert info == {"omp_session_id": "abc", "model": "m", "cwd": "/tmp"}
    with pytest.raises(ValueError, match="explicit"):
        inspect_adoption(["omp", "--continue"], "/tmp")


def test_runtime_persists_negotiation_and_correlated_response(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    store = SessionStore(paths)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", platform="telegram", chat="42", topic="7", allowed_users=["9"])
    store.save(session)
    runtime = Runtime(session, paths, omp_path="fake")
    startup = runtime.startup_frames()
    assert startup[0]["type"] == "negotiate_protocol" and startup[-1]["message"] == "mission"
    q = runtime.on_event({"type":"extension_ui_request","id":"q1","method":"select","title":"Pick","options":[{"label":"A","description":"alpha"}]}, now=1)
    assert q and "question_id=q1" in q["text"]
    rejected = runtime.accept_inbound({"event_id":"e0","question_id":"q1","platform":"telegram","chat":"42","topic":"WRONG","user":"9","answer":"1"}, now=2)
    assert rejected is None
    accepted = runtime.accept_inbound({"event_id":"e1","question_id":"q1","platform":"telegram","chat":"42","topic":"7","user":"9","answer":"1"}, now=2)
    assert accepted == {"type":"extension_ui_response","id":"q1","value":"A"}
    reloaded = SessionStore(paths).load("demo")
    assert reloaded.last_activity >= 2


def test_runtime_rejects_expired_question(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",question_ttl=1)
    runtime.on_event({"type":"extension_ui_request","id":"q","title":"x"},now=1)
    assert runtime.accept_inbound({"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"yes"},now=3) is None


def test_runtime_auto_answers_only_safe_question(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",auto_answer_safe=True)
    response=runtime.on_event({"type":"extension_ui_request","id":"q","title":"Continue","options":[{"label":"Yes","recommended":True,"reversible":True}]},now=1)
    assert response["rpc"] == {"type":"extension_ui_response","id":"q","value":"Yes"}
