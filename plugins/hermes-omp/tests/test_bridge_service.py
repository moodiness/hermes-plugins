from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_omp.bridge import FileInbox, HermesSendBridge
from hermes_omp.service import LaunchdBackend, ServiceSnapshot, SystemdBackend, WindowsTaskBackend, backend_for


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


@pytest.mark.parametrize(("policy","restart_count"), [("never",None),("on-failure","3"),("always","999")])
def test_windows_task_xml_generation_honors_restart_policy(policy: str, restart_count: str | None) -> None:
    text = WindowsTaskBackend(Path("C:/x")).definition("demo", ["python.exe", "-m", "hermes_omp.runtime", "demo"], "C:/Work Area", policy)
    assert "<Command>python.exe</Command>" in text and "<WorkingDirectory>C:/Work Area</WorkingDirectory>" in text
    assert "-m hermes_omp.runtime demo" in text
    if restart_count is None: assert "<RestartOnFailure>" not in text
    else: assert f"<Count>{restart_count}</Count>" in text


def test_backend_selection() -> None:
    assert isinstance(backend_for("darwin", Path("/tmp")), LaunchdBackend)
    assert isinstance(backend_for("linux", Path("/tmp")), SystemdBackend)
    assert isinstance(backend_for("win32", Path("/tmp")), WindowsTaskBackend)
    with pytest.raises(ValueError): backend_for("plan9", Path("/tmp"))


def test_backends_expose_their_definition_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert LaunchdBackend(tmp_path).definition_path("Demo") == (
        tmp_path / "Library" / "LaunchAgents" / "ai.hermes.omp.demo.plist"
    )
    assert SystemdBackend(tmp_path).definition_path("Demo") == (
        tmp_path / ".config" / "systemd" / "user" / "hermes-omp-demo.service"
    )
    assert WindowsTaskBackend(tmp_path).definition_path("Demo") == (
        tmp_path / "services" / "hermes-omp-demo.xml"
    )


def test_service_restore_re_registers_exact_prior_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    for backend_type, expected in [
        (LaunchdBackend, ("bootout", "bootstrap")),
        (SystemdBackend, ("disable", "daemon-reload", "enable")),
        (WindowsTaskBackend, ("/Delete", "/Create")),
    ]:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("Result", (), {"returncode": 0})()

        backend = backend_type(tmp_path, runner=runner)
        path = backend.definition_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"opaque prior bytes")
        snapshot = backend.snapshot("demo")
        assert snapshot == ServiceSnapshot(path, b"opaque prior bytes", True)
        path.write_bytes(b"new bytes")

        backend.restore("demo", snapshot)

        assert path.read_bytes() == b"opaque prior bytes"
        flattened = [part for argv in calls for part in argv]
        for token in expected:
            assert token in flattened
