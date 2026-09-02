from __future__ import annotations

import argparse
import json
import os
import stat
import threading
from pathlib import Path

import pytest

import hermes_omp.cli as cli
from hermes_omp.cli import main
from hermes_omp.core import Outbox, Paths, SessionStore


def invoke(tmp_path: Path, capsys, *args: str) -> tuple[int, str, str]:
    os.environ["HERMES_HOME"] = str(tmp_path)
    rc = main(list(args))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def create(tmp_path: Path, capsys, name: str = "demo") -> None:
    rc, _, _ = invoke(tmp_path, capsys, "create", name, "--cwd", str(tmp_path), "--model", "m", "--mission", "go", "--platform", "telegram", "--chat", "42", "--allowed-user", "9", "--omp-path", "/bin/true", "--no-install")
    assert rc == 0


def test_every_user_command_accepts_json_and_errors_are_stable(tmp_path: Path, capsys) -> None:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, child in action.choices.items():
        if name in {"run", "inbound"}:
            continue
        assert any("--json" in option for item in child._actions for option in item.option_strings), name
    rc, out, err = invoke(tmp_path, capsys, "status", "missing", "--json")
    assert rc == cli.EXIT_NOT_FOUND and err == ""
    payload = json.loads(out)
    assert payload["ok"] is False and payload["error"]["code"] == "not_found"


def test_events_inspects_redacted_queues_with_filters(tmp_path: Path, capsys) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    prompts = Outbox(paths.run / "demo.prompts.json", max_attempts=1)
    prompts.enqueue("prompt-a", {"message": "token=secret"})
    prompts.fail("prompt-a", error="password=hunter2")
    outbound = Outbox(paths.outbox / "demo.json")
    outbound.enqueue("out-a", {"text": "hello", "platform": "telegram"})
    outbound.ack("out-a")
    inbox = paths.inbox / "demo"
    (inbox / "processed").mkdir(parents=True)
    (inbox / "rejected").mkdir(parents=True)
    (inbox / "rejected" / "bad.json").write_text(json.dumps({"event_id": "bad", "authorization": "Bearer abc"}))
    rc, out, _ = invoke(tmp_path, capsys, "events", "demo", "--queue", "prompt,outbound,inbound", "--status", "dead,delivered,rejected", "--limit", "10", "--json")
    assert rc == 0
    payload = json.loads(out)
    assert {event["status"] for event in payload["events"]} == {"dead", "delivered", "rejected"}
    assert "secret" not in out and "hunter2" not in out and "Bearer abc" not in out


def test_retry_dead_letters_is_explicit_idempotent_and_outbound_only(tmp_path: Path, capsys) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    outbox = Outbox(paths.outbox / "demo.json", max_attempts=1)
    outbox.enqueue("dead", {"platform": "telegram", "chat": "42", "text": "x"})
    outbox.fail("dead", error="offline")
    rc, out, _ = invoke(tmp_path, capsys, "retry", "demo", "dead", "--yes", "--json")
    assert rc == 0 and json.loads(out)["retried"] == ["dead"]
    reloaded = Outbox(paths.outbox / "demo.json")
    assert reloaded.items[0].state == "pending" and reloaded.items[0].attempts == 0
    rc, out, _ = invoke(tmp_path, capsys, "retry", "demo", "dead", "--yes", "--json")
    assert rc == 0 and json.loads(out)["retried"] == []


def test_export_import_dry_run_conflicts_validation_and_secret_exclusion(tmp_path: Path, capsys) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    (paths.run / "demo.owner").write_text(json.dumps({"pid": os.getpid(), "token": "secret"}))
    archive = tmp_path / "archive.json"
    rc, out, _ = invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")
    assert rc == 0 and archive.exists()
    exported = archive.read_text()
    assert '"archive_version": 1' in exported and '"supervisor_pid": 0' in exported and '"omp_pid": 0' in exported
    assert "secret" not in exported and ".owner" not in exported
    rc, out, _ = invoke(tmp_path, capsys, "import", str(archive), "--conflict", "rename", "--dry-run", "--json")
    plan = json.loads(out)
    assert rc == 0 and plan["dry_run"] is True and plan["name"] == "demo-2"
    assert not (paths.sessions / "demo-2.json").exists()
    rc, out, _ = invoke(tmp_path, capsys, "import", str(archive), "--conflict", "rename", "--no-install", "--json")
    assert rc == 0 and SessionStore(paths).load("demo-2").id != SessionStore(paths).load("demo").id
    archive.write_text('{"archive_version": 99}')
    rc, out, _ = invoke(tmp_path, capsys, "import", str(archive), "--json")
    assert rc == cli.EXIT_VALIDATION and json.loads(out)["error"]["code"] == "validation"


def test_imported_omp_id_cannot_be_duplicated_by_rename(tmp_path: Path, capsys) -> None:
    rc, _, _ = invoke(
        tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path),
        "--model", "m", "--mission", "go", "--resume", "owned-id",
        "--omp-path", "/bin/true", "--no-install",
    )
    assert rc == 0
    archive = tmp_path / "archive.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")[0] == 0

    rc, out, _ = invoke(
        tmp_path, capsys, "import", str(archive), "--conflict", "rename",
        "--no-install", "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"
    assert "already owned" in json.loads(out)["error"]["message"]
    assert not (Paths.discover().sessions / "demo-2.json").exists()


def test_replace_import_restores_all_tracked_files_on_failure(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    runtime_path = paths.run / "demo.runtime.json"
    runtime_path.write_text('{"original": true}\n')
    archive = tmp_path / "archive.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")[0] == 0
    value = json.loads(archive.read_text())
    value["session"]["model"] = "replacement"
    value["omp_path"] = "/replacement/omp"
    value["runtime"] = {"replacement": True}
    archive.write_text(json.dumps(value))
    targets = [
        paths.sessions / "demo.json",
        paths.run / "demo.omp-path",
        runtime_path,
    ]
    originals = {target: target.read_bytes() for target in targets}


    class BrokenBackend:
        def definition_path(self, name):
            return tmp_path / f"{name}.service"

        def definition(self, *args, **kwargs):
            return {"fake": True}

        def install(self, *args, **kwargs):
            raise RuntimeError("install failed")

        def remove(self, name):
            pass

    monkeypatch.setattr(cli, "backend_for", lambda **kwargs: BrokenBackend())

    with pytest.raises(RuntimeError, match="install failed"):
        invoke(
            tmp_path, capsys, "import", str(archive), "--conflict", "replace",
            "--json",
        )
    assert {target: target.read_bytes() for target in targets} == originals



def test_concurrent_rename_imports_reserve_distinct_names(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(tmp_path, capsys)
    archive = tmp_path / "rename.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")[0] == 0
    original_persist = cli._persist_and_install
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def coordinated_persist(*args, **kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            number = call_count
        if number == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(cli, "_persist_and_install", coordinated_persist)
    results: list[int] = []
    errors: list[BaseException] = []

    def run_import() -> None:
        try:
            results.append(cli.main([
                "import", str(archive), "--conflict", "rename", "--no-install",
            ]))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_import)
    second = threading.Thread(target=run_import)
    first.start()
    assert first_entered.wait(2)
    second.start()
    overlapped = second_entered.wait(0.25)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert not overlapped
    assert errors == [] and sorted(results) == [0, 0]
    paths = Paths.discover()
    assert (paths.sessions / "demo-2.json").exists()
    assert (paths.sessions / "demo-3.json").exists()


def test_failed_replace_cannot_rollback_over_later_success(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(tmp_path, capsys)
    base_archive = tmp_path / "base.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(base_archive), "--json")[0] == 0

    def replacement_archive(path: Path, model: str) -> None:
        value = json.loads(base_archive.read_text())
        value["session"]["model"] = model
        value["omp_path"] = f"/{model}/omp"
        value["runtime"] = {"model": model}
        path.write_text(json.dumps(value))

    failing_archive = tmp_path / "failing.json"
    winner_archive = tmp_path / "winner.json"
    replacement_archive(failing_archive, "failing")
    replacement_archive(winner_archive, "winner")
    failing_entered = threading.Event()
    release_failing = threading.Event()
    winner_entered = threading.Event()

    class Backend:
        def definition_path(self, name):
            return tmp_path / f"{name}.service"

        def definition(self, *args, **kwargs):
            return {"fake": True}

        def install(self, *args, **kwargs):
            pass

        def remove(self, name):
            pass

    monkeypatch.setattr(cli, "backend_for", lambda **kwargs: Backend())

    def coordinated_install(session, no_install, start, paths):
        if session.model == "failing":
            failing_entered.set()
            assert release_failing.wait(2)
            raise RuntimeError("injected install failure")
        winner_entered.set()

    monkeypatch.setattr(cli, "_install", coordinated_install)
    results: list[object] = []

    def run_import(path: Path) -> None:
        try:
            results.append(cli.main(["import", str(path), "--conflict", "replace"]))
        except BaseException as exc:
            results.append(exc)

    failing = threading.Thread(target=run_import, args=(failing_archive,))
    winner = threading.Thread(target=run_import, args=(winner_archive,))
    failing.start()
    assert failing_entered.wait(2)
    winner.start()
    overlapped = winner_entered.wait(0.25)
    release_failing.set()
    failing.join(2)
    winner.join(2)

    assert not failing.is_alive() and not winner.is_alive()
    assert not overlapped
    assert any(isinstance(result, RuntimeError) for result in results)
    assert 0 in results
    paths = Paths.discover()
    assert SessionStore(paths).load("demo").model == "winner"
    assert (paths.run / "demo.omp-path").read_text() == "/winner/omp\n"
    assert json.loads((paths.run / "demo.runtime.json").read_text()) == {"model": "winner"}


def test_replace_service_failure_restores_exact_prior_definition(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(tmp_path, capsys)
    archive = tmp_path / "replace.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")[0] == 0
    value = json.loads(archive.read_text())
    value["session"]["restart_policy"] = "always"
    archive.write_text(json.dumps(value))
    service_path = tmp_path / "demo.service"
    original_definition = b"opaque pre-existing definition\n"
    service_path.write_bytes(original_definition)
    installs: list[str] = []

    class Backend:
        def definition_path(self, name):
            return service_path

        def definition(self, *args, **kwargs):
            return {"fake": True}

        def install(self, name, command, cwd, restart_policy, activate):
            installs.append(restart_policy)
            service_path.write_bytes(b"partial replacement")
            raise RuntimeError("injected replacement install failure")

        def remove(self, name):
            raise AssertionError("pre-existing definition must be restored, not removed")

    monkeypatch.setattr(cli, "backend_for", lambda **kwargs: Backend())

    with pytest.raises(RuntimeError, match="replacement install failure"):
        invoke(
            tmp_path, capsys, "import", str(archive), "--conflict", "replace",
            "--json",
        )

    assert installs == ["always"]
    assert service_path.read_bytes() == original_definition
    assert SessionStore(Paths.discover()).load("demo").restart_policy == "on-failure"


def test_replace_service_failure_preserves_prior_absence(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(tmp_path, capsys)
    archive = tmp_path / "replace.json"
    assert invoke(tmp_path, capsys, "export", "demo", str(archive), "--json")[0] == 0
    service_path = tmp_path / "demo.service"
    removed: list[str] = []

    class Backend:
        def definition_path(self, name):
            return service_path

        def definition(self, *args, **kwargs):
            return {"fake": True}

        def install(self, *args, **kwargs):
            service_path.write_bytes(b"partial fresh definition")
            raise RuntimeError("injected fresh install failure")

        def remove(self, name):
            removed.append(name)

    monkeypatch.setattr(cli, "backend_for", lambda **kwargs: Backend())

    with pytest.raises(RuntimeError, match="fresh install failure"):
        invoke(
            tmp_path, capsys, "import", str(archive), "--conflict", "replace",
            "--json",
        )

    assert removed == ["demo"]
    assert not service_path.exists()


def test_update_requires_explicit_restart_for_live_session_and_dry_run_writes_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    original = (paths.sessions / "demo.json").read_bytes()
    rc, out, _ = invoke(tmp_path, capsys, "update", "demo", "--model", "new", "--mission", "next", "--dry-run", "--json")
    assert rc == 0 and json.loads(out)["changes"]["model"] == {"from": "m", "to": "new"}
    assert (paths.sessions / "demo.json").read_bytes() == original
    (paths.run / "demo.owner").write_text(json.dumps({"pid": os.getpid(), "session_id": SessionStore(paths).load("demo").id, "token": "x"}))
    rc, out, _ = invoke(tmp_path, capsys, "update", "demo", "--model", "new", "--json")
    assert rc == cli.EXIT_CONFLICT and json.loads(out)["error"]["code"] == "conflict"


def test_create_and_adopt_dry_run_print_definition_without_writes(tmp_path: Path, capsys) -> None:
    rc, out, _ = invoke(tmp_path, capsys, "create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "x", "--omp-path", "/bin/true", "--dry-run", "--json")
    payload = json.loads(out)
    assert rc == 0 and payload["dry_run"] and payload["service_definition"]
    assert not (tmp_path / "omp").exists()
    inspection = tmp_path / "inspection.json"
    inspection.write_text(json.dumps({"argv": ["omp", "--resume", "sid", "--model", "m"], "cwd": str(tmp_path)}))
    rc, out, _ = invoke(tmp_path, capsys, "adopt", "adopted", "--inspection", str(inspection), "--mission", "x", "--omp-path", "/bin/true", "--dry-run", "--json")
    assert rc == 0 and json.loads(out)["session"]["omp_session_id"] == "sid"
    assert not (tmp_path / "omp").exists()


def test_logs_support_since_level_and_bounded_follow(tmp_path: Path, capsys, monkeypatch) -> None:
    create(tmp_path, capsys)
    path = Paths.discover().logs / "demo.jsonl"
    path.write_text('\n'.join([json.dumps({"timestamp": 10, "level": "info", "message": "old"}), json.dumps({"timestamp": 20, "level": "error", "message": "boom"})]) + '\n')
    rc, out, _ = invoke(tmp_path, capsys, "logs", "demo", "--since", "15", "--level", "error", "--json")
    assert rc == 0 and [x["message"] for x in json.loads(out)["entries"]] == ["boom"]
    rc, out, _ = invoke(tmp_path, capsys, "logs", "demo", "--follow", "--poll-interval", "0", "--max-polls", "1", "--json")
    assert rc == 0 and json.loads(out)["polls"] == 1


def test_doctor_fix_repairs_only_safe_inactive_state(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_OMP_BINARY", "/bin/true")
    monkeypatch.setenv("HERMES_OMP_HERMES", "/bin/true")
    paths = Paths(tmp_path / "omp")
    paths.ensure()
    os.chmod(paths.logs, 0o755)
    (paths.run / "stale.owner").write_text(json.dumps({"pid": 99999999, "session_id": "x", "token": "do-not-report"}))
    rc, out, _ = invoke(tmp_path, capsys, "doctor", "--fix", "--dry-run", "--json")
    report = json.loads(out)
    assert rc == 0 and report["dry_run"] and any(x["action"] == "chmod" for x in report["repairs"])
    assert stat.S_IMODE(paths.logs.stat().st_mode) == 0o755 and (paths.run / "stale.owner").exists()
    rc, out, _ = invoke(tmp_path, capsys, "doctor", "--fix", "--json")
    assert rc == 0 and stat.S_IMODE(paths.logs.stat().st_mode) == 0o700 and not (paths.run / "stale.owner").exists()
    assert "do-not-report" not in out


def test_status_list_health_queue_depths_last_error_activity_and_config_template(tmp_path: Path, capsys) -> None:
    create(tmp_path, capsys)
    paths = Paths.discover()
    outbox = Outbox(paths.outbox / "demo.json", max_attempts=1)
    outbox.enqueue("x", {"text": "x"}); outbox.fail("x", error="offline")
    rc, out, _ = invoke(tmp_path, capsys, "status", "demo", "--json")
    status = json.loads(out)
    assert rc == 0 and status["health"] in {"healthy", "degraded", "stopped"}
    assert status["queues"]["outbound_dead"] == 1 and status["last_error"] == "offline" and "last_activity" in status
    rc, out, _ = invoke(tmp_path, capsys, "list", "--json")
    assert json.loads(out)["sessions"][0]["queues"]["outbound_dead"] == 1
    rc, out, _ = invoke(tmp_path, capsys, "config", "validate", "demo", "--json")
    assert rc == 0 and json.loads(out)["valid"] is True
    rc, out, _ = invoke(tmp_path, capsys, "config", "template", "--json")
    assert rc == 0 and json.loads(out)["template"]["restart_policy"] == "on-failure"


def test_completion_generates_standalone_shell_scripts(tmp_path: Path, capsys) -> None:
    for shell in ("bash", "zsh", "fish"):
        rc, out, _ = invoke(tmp_path, capsys, "completion", shell, "--json")
        assert rc == 0 and "hermes-omp" in json.loads(out)["script"]
