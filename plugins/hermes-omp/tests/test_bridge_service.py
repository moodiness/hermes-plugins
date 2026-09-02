from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
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
    assert data["ProgramArguments"][3] == "demo"
    assert data["ProgramArguments"][-2:] == ["--service-log", str(tmp_path / "logs" / "demo.service.jsonl")]
    assert data["StandardOutPath"] == data["StandardErrorPath"] == "/dev/null"


def test_systemd_user_unit_generation_quotes_literal_values() -> None:
    command = [
        '/opt/Python "odd"/bin/python',
        "-m",
        "hermes_omp.runtime",
        "demo",
        "--root",
        '/tmp/state 100%/$HOME/"quoted"/omp',
    ]
    text = SystemdBackend(Path("/tmp")).definition(
        "demo", command, '/work/space "quote"/100%', "always"
    )

    assert 'WorkingDirectory="/work/space \\"quote\\"/100%%"' in text
    assert (
        'ExecStart="/opt/Python \\"odd\\"/bin/python" "-m" '
        '"hermes_omp.runtime" "demo" "--root" '
        '"/tmp/state 100%%/$$HOME/\\"quoted\\"/omp"'
    ) in text
    assert "Restart=on-failure" in text


@pytest.mark.parametrize("hostile", ["line\nfeed", "carriage\rreturn", "nul\0byte"])
def test_systemd_definition_rejects_control_characters(hostile: str) -> None:
    backend = SystemdBackend(Path("/tmp"))

    with pytest.raises(ValueError, match="CR, LF, or NUL"):
        backend.definition("demo", ["python", hostile], "/work", "never")
    with pytest.raises(ValueError, match="CR, LF, or NUL"):
        backend.definition("demo", ["python"], hostile, "never")


@pytest.mark.parametrize(("policy","restart_count"), [("never",None),("on-failure","999"),("always","999")])
def test_windows_task_xml_generation_honors_restart_policy(policy: str, restart_count: str | None) -> None:
    command = [
        'C:\\Program Files\\Python "Special"\\python.exe',
        "-m",
        "hermes_omp.runtime",
        "demo with spaces",
        "--root",
        'C:\\State Root\\100% "quoted"\\omp',
    ]
    text = WindowsTaskBackend(Path("C:/x")).definition(
        "demo", command, "C:/Work & Area", policy
    )
    root = ET.fromstring(text)
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert root.findtext(".//task:Command", namespaces=namespace) == command[0]
    expected = command + ["--service-log", "C:/x/logs/demo.service.jsonl"]
    assert root.findtext(".//task:Arguments", namespaces=namespace) == subprocess.list2cmdline(expected[1:])
    assert root.findtext(".//task:WorkingDirectory", namespaces=namespace) == "C:/Work & Area"
    if restart_count is None: assert "<RestartOnFailure>" not in text
    else: assert f"<Count>{restart_count}</Count>" in text


def test_launchd_stop_boots_out_and_start_bootstraps_before_kickstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hermes_omp.service.os.getuid", lambda: 501, raising=False)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()

    backend = LaunchdBackend(tmp_path / "omp", runner=runner)
    definition_path = backend.definition_path("Demo")
    domain = "gui/501"

    backend.stop("Demo")
    backend.start("Demo")

    assert calls == [
        (["launchctl", "bootout", domain, str(definition_path)], {"check": False}),
        (["launchctl", "bootstrap", domain, str(definition_path)], {"check": False}),
        (["launchctl", "kickstart", "-k", f"{domain}/ai.hermes.omp.demo"], {"check": True}),
    ]


def test_systemd_install_enables_and_remove_disables_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()

    backend = SystemdBackend(tmp_path / "omp", runner=runner)
    path = backend.install("Demo", ["python", "-m", "hermes_omp.runtime", "demo"], "/work", "always")
    assert path.exists()
    backend.remove("Demo")

    unit = "hermes-omp-demo.service"
    assert calls == [
        (["systemctl", "--user", "daemon-reload"], {"check": True}),
        (["systemctl", "--user", "enable", unit], {"check": True}),
        (["systemctl", "--user", "disable", "--now", unit], {"check": False}),
        (["systemctl", "--user", "daemon-reload"], {"check": False}),
    ]
    assert not path.exists()


def test_windows_install_writes_utf8_and_remove_deletes_definition(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()

    backend = WindowsTaskBackend(tmp_path / "omp", runner=runner)
    command = ["python.exe", "-m", "hermes_omp.runtime", "démø"]
    path = backend.install("Demo", command, "C:/Wörk & Area", "never", activate=False)

    assert path.read_bytes().decode("utf-8") == backend.definition("Demo", command, "C:/Wörk & Area", "never")
    assert path.read_bytes().startswith(b'<?xml version="1.0" encoding="UTF-8"?>')

    backend.remove("Demo")

    assert calls == [
        (["schtasks", "/Delete", "/TN", "HermesOMP-demo", "/F"], {"check": False})
    ]
    assert not path.exists()


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
    monkeypatch.setattr("hermes_omp.service.os.getuid", lambda: 501, raising=False)

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


@pytest.mark.parametrize("backend_type", [LaunchdBackend, SystemdBackend, WindowsTaskBackend])
def test_service_snapshot_probe_suppresses_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_type
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("hermes_omp.service.os.getuid", lambda: 501, raising=False)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 1})()

    backend_type(tmp_path, runner=runner).snapshot("demo")

    assert calls[0][1]["capture_output"] is True
