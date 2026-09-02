from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from hermes_omp import cli
from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.runtime import Runtime


def test_session_notification_controls_default_and_persist(tmp_path: Path) -> None:
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    assert session.notifications == {
        "question": True,
        "error": True,
        "milestone": True,
        "completion": True,
        "restart": True,
    }
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(session)
    assert SessionStore(paths).load("demo").notifications == session.notifications


def test_notification_controls_suppress_selected_kind_and_deduplicate_durably(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        notifications={"milestone": False},
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true")
    assert runtime.notification("milestone", "same", "halfway") is None
    first = runtime.notification("error", "same", "failed")
    assert first and first["event_id"].startswith("notification-error-")
    runtime.commit_notification(first["dedup_key"])
    assert runtime.notification("error", "same", "failed") is None
    restarted = Runtime(SessionStore(paths).load("demo"), paths, omp_path="/bin/true")
    assert restarted.notification("error", "same", "failed") is None


def test_duration_budget_is_enforced_and_reported_truthfully(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo", cwd=str(tmp_path), model="m", mission="mission", max_duration_seconds=10
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true", started_at=100)
    assert runtime.budget_status(now=109)["duration"]["state"] == "within_limit"
    assert runtime.budget_status(now=111)["duration"]["state"] == "exceeded"
    assert runtime.should_stop(now=111) == "duration_exceeded"


def test_restart_window_and_cooldown_are_durable(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        max_restarts=2,
        restart_window_seconds=60,
        restart_cooldown_seconds=30,
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true")
    assert runtime.record_restart(now=100)["allowed"] is True
    assert runtime.record_restart(now=101)["allowed"] is False
    assert runtime.restart_status(now=131)["allowed"] is True
    assert runtime.record_restart(now=131)["allowed"] is True
    assert runtime.restart_status(now=132)["allowed"] is False
    restarted = Runtime(SessionStore(paths).load("demo"), paths, omp_path="/bin/true")
    assert restarted.restart_status(now=132)["allowed"] is False
    assert restarted.restart_status(now=200)["allowed"] is True


def test_usage_caps_fail_closed_without_trustworthy_public_rpc(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        max_tokens=100,
        max_cost_usd=1.5,
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true")
    status = runtime.budget_status()
    assert status["tokens"] == {"state": "unavailable", "enforceable": False, "reason": "trustworthy_public_rpc_usage_unavailable"}
    assert status["cost"] == {"state": "unavailable", "enforceable": False, "reason": "trustworthy_public_rpc_usage_unavailable"}
    assert runtime.should_start() is False


def test_usage_caps_enforce_only_declared_trustworthy_public_rpc(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo", cwd=str(tmp_path), model="m", mission="mission", max_tokens=100
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true", usage_rpc_trustworthy=True)
    runtime.on_event({"type": "usage", "source": "public_rpc", "total_tokens": 101})
    assert runtime.budget_status()["tokens"]["state"] == "exceeded"
    assert runtime.should_stop() == "token_limit_exceeded"


def test_transition_log_is_bounded_structured_redacted_and_local(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true", transition_max_bytes=700)
    for index in range(30):
        runtime.transition("running", "milestone", {"token": f"secret-{index}", "message": "x" * 80}, now=index)
    path = paths.logs / "demo.transitions.ndjson"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.stat().st_size <= 700
    assert records
    assert all(set(record) == {"timestamp", "session", "from", "to", "reason", "details"} for record in records)
    assert "secret-" not in path.read_text()
    assert runtime.telemetry_enabled is False


def test_dashboard_snapshot_is_bounded_redacted_read_only(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="token=secret")
    SessionStore(paths).create(session)
    question = paths.run / "demo.question.json"
    question.write_text(json.dumps({"id": "q", "title": "password=hidden"}))
    snapshot = cli.dashboard_snapshot(paths, log_limit=3)
    assert snapshot["read_only"] is True
    assert snapshot["actions_require_confirmation"] is True
    assert len(snapshot["sessions"]) == 1
    assert len(snapshot["logs"]) <= 3
    assert "secret" not in json.dumps(snapshot)
    assert "hidden" not in json.dumps(snapshot)


def test_desktop_plugin_contract_uses_public_sdk_and_safe_backend() -> None:
    root = Path(__file__).parents[1]
    plugin = (root / "plugin" / "desktop" / "plugin.js").read_text()
    backend = (root / "plugin" / "dashboard" / "plugin_api.py").read_text()
    manifest = (root / "plugin" / "plugin.yaml").read_text()
    assert "@hermes/plugin-sdk" in plugin
    assert "defaultEnabled: false" in plugin
    assert "ctx.rest('/snapshot'" in plugin
    assert "ConfirmDialog" in plugin
    assert "ctx.rest('/action'" in plugin
    assert "subprocess" not in plugin
    assert "dashboard/plugin_api.py" in manifest
    assert "confirmation" in backend


def test_cli_persists_notification_and_budget_controls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = Paths(tmp_path / "omp")
    args = cli.build_parser().parse_args([
        "create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "mission",
        "--no-notify", "milestone", "--max-duration", "60", "--max-restarts", "2",
        "--restart-window", "120", "--restart-cooldown", "10", "--max-tokens", "0",
        "--max-cost-usd", "0", "--omp-path", "/bin/true", "--no-install", "--json",
    ])
    assert cli.dispatch_namespace(args, paths) == 0
    capsys.readouterr()
    session = SessionStore(paths).load("demo")
    assert session.notifications["milestone"] is False
    assert session.max_duration_seconds == 60
    assert session.max_restarts == 2
    assert session.restart_window_seconds == 120
    assert session.restart_cooldown_seconds == 10


def test_cli_rejects_negative_budgets_before_persisting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = Paths(tmp_path / "omp")
    args = cli.build_parser().parse_args([
        "create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "mission",
        "--max-duration", "-1", "--omp-path", "/bin/true", "--no-install", "--json",
    ])
    assert cli.dispatch_namespace(args, paths) == cli.EXIT_VALIDATION
    assert not (paths.sessions / "demo.json").exists()
    assert "non-negative" in capsys.readouterr().out


def test_root_pytest_configuration_prepends_worktree_source() -> None:
    root = Path(__file__).parents[3]
    conftest = (root / "conftest.py").read_text()
    assert "plugins/hermes-omp/src" in conftest


def test_dashboard_action_contract_requires_confirmation_and_never_executes(tmp_path: Path) -> None:
    import importlib.util

    path = Path(__file__).parents[1] / "plugin" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("omp_dashboard_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="confirmation"):
        module.post_action({"action": "restart", "session": "demo"})
    result = module.post_action({"action": "restart", "session": "demo", "confirmation": True})
    assert result == {"confirmed": True, "command": ["hermes", "omp", "restart", "demo"], "execute": False}
    with pytest.raises(ValueError, match="unsupported"):
        module.post_action({"action": "remove", "session": "demo", "confirmation": True})
