from __future__ import annotations

import argparse
import json
import os
import stat
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
