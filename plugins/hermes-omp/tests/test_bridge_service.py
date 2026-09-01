from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_omp.bridge import FileInbox, HermesSendBridge
from hermes_omp.service import LaunchdBackend, SystemdBackend, WindowsTaskBackend, backend_for


def test_hermes_send_uses_stdin_and_no_secret_or_message_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    bridge = HermesSendBridge(hermes="/fake/hermes", runner=run)
    bridge.deliver({"platform": "telegram", "chat": "42", "topic": "7", "text": "super secret body"})
    argv, kwargs = calls[0]
    assert argv[:2] == ["/fake/hermes", "send"]
    assert "super secret body" not in " ".join(argv)
    assert kwargs["input"] == "super secret body"
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)


def test_file_inbox_is_replaceable_public_contract_and_exactly_once(tmp_path: Path) -> None:
    inbox = FileInbox(tmp_path)
    path = inbox.submit({"event_id": "e1", "question_id": "q1", "answer": "1"})
    assert path.suffix == ".json"
    assert inbox.poll() == [{"event_id": "e1", "question_id": "q1", "answer": "1"}]
    inbox.ack("e1")
    assert inbox.poll() == []


def test_launchd_definition_has_safe_restart_and_runtime_command(tmp_path: Path) -> None:
    backend = LaunchdBackend(tmp_path)
    data = backend.definition("demo", ["python", "-m", "hermes_omp.runtime", "demo"], str(tmp_path), "on-failure")
    assert data["Label"] == "ai.hermes.omp.demo"
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ProgramArguments"][-1] == "demo"


def test_systemd_user_unit_generation() -> None:
    text = SystemdBackend(Path("/tmp")).definition("demo", ["python", "-m", "hermes_omp.runtime", "demo"], "/work", "always")
    assert "Restart=always" in text and "WorkingDirectory=/work" in text and "ExecStart=python -m hermes_omp.runtime demo" in text


def test_windows_task_xml_generation_quotes_arguments() -> None:
    text = WindowsTaskBackend(Path("C:/x")).definition("demo", ["python.exe", "-m", "hermes_omp.runtime", "demo"], "C:/Work Area", "on-failure")
    assert "<Command>python.exe</Command>" in text and "<WorkingDirectory>C:/Work Area</WorkingDirectory>" in text
    assert "-m hermes_omp.runtime demo" in text


def test_backend_selection() -> None:
    assert isinstance(backend_for("darwin", Path("/tmp")), LaunchdBackend)
    assert isinstance(backend_for("linux", Path("/tmp")), SystemdBackend)
    assert isinstance(backend_for("win32", Path("/tmp")), WindowsTaskBackend)
    with pytest.raises(ValueError): backend_for("plan9", Path("/tmp"))
