from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.runtime import Runtime, acquire_owner_lock, build_omp_command, inspect_adoption


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
    assert rejected.terminal is True
    accepted = runtime.accept_inbound({"event_id":"e1","question_id":"q1","platform":"telegram","chat":"42","topic":"7","user":"9","answer":"1"}, now=2)
    assert accepted.response == {"type":"extension_ui_response","id":"q1","value":"A"}
    reloaded = SessionStore(paths).load("demo")
    assert reloaded.last_activity >= 2


def test_runtime_rejects_expired_question(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",question_ttl=1)
    runtime.on_event({"type":"extension_ui_request","id":"q","title":"x"},now=1)
    assert runtime.accept_inbound({"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"yes"},now=3).terminal is True


def test_runtime_auto_answers_only_safe_question(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",auto_answer_safe=True)
    response=runtime.on_event({"type":"extension_ui_request","id":"q","title":"Continue","options":[{"label":"Yes","recommended":True,"reversible":True}]},now=1)
    assert response["rpc"] == {"type":"extension_ui_response","id":"q","value":"Yes"}


def test_runtime_reloads_pending_question_and_replay_ids(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); first=Runtime(session,paths,omp_path="fake")
    first.on_event({"type":"extension_ui_request","id":"q","method":"select","title":"Pick","options":[{"label":"A"}]},now=1)
    accepted={"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"1"}
    assert first.accept_inbound(accepted,now=2).response["value"] == "A"
    second=Runtime(SessionStore(paths).load("demo"),paths,omp_path="fake")
    assert second.accept_inbound(accepted,now=3).terminal is True
    first.on_event({"type":"extension_ui_request","id":"q2","method":"input","title":"Text"},now=4)
    third=Runtime(SessionStore(paths).load("demo"),paths,omp_path="fake")
    assert third.question and third.question.id == "q2" and third.question.method == "input"


def test_inbound_outcomes_distinguish_retryable_and_terminal(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake")
    event={"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"1"}
    assert runtime.accept_inbound(event,now=1).retryable is True
    runtime.on_event({"type":"extension_ui_request","id":"q","title":"Pick","options":[{"label":"A"}]},now=2)
    invalid={**event,"event_id":"bad","answer":"9"}
    assert runtime.accept_inbound(invalid,now=3).terminal is True
    assert runtime.accept_inbound({**event,"event_id":"wrong","topic":"other"},now=3).terminal is True


def test_owner_lock_recovers_dead_pid_but_never_replaces_live_or_foreign_lock(tmp_path: Path) -> None:
    lock=tmp_path/"owner"
    lock.write_text(json.dumps({"pid":99999999,"session_id":"sid"}))
    fd, token=acquire_owner_lock(lock,"sid")
    os.close(fd)
    assert json.loads(lock.read_text())["token"] == token
    with pytest.raises(RuntimeError,match="already owned"):
        acquire_owner_lock(lock,"sid")
    lock.write_text(json.dumps({"pid":99999999,"session_id":"other","token":"x"}))
    with pytest.raises(RuntimeError,match="different session"):
        acquire_owner_lock(lock,"sid")
