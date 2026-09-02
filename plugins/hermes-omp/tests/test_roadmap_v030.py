from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import stat


import pytest

from hermes_omp import cli
from hermes_omp.core import Outbox, Paths, Session, SessionStore, redact
from hermes_omp.runtime import Runtime


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))

def invoke(paths: Paths, capsys: pytest.CaptureFixture[str], *argv: str):
    rc = cli.dispatch_namespace(parse(*argv), paths)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def legacy_record(tmp_path: Path) -> dict[str, object]:
    return {
        "cwd": str(tmp_path),
        "model": "legacy-model",
        "mission": "legacy mission",
        "platform": "telegram",
        "chat": "42",
        "topic": "7",
        "allowed_users": ["9"],
        "omp_session_id": "legacy-remote-id",
        "omp_path": "/bin/true",
        "omp_options": ["--reasoning", "high"],
        "restart_policy": "never",
        "policy_profile": "night",
    }


def test_parser_exposes_complete_v030_commands():
    commands = {"migrate-legacy", "watch", "diagnose", "clone"}
    for command in commands:
        assert parse(command, "demo").command == command


def test_migrate_legacy_defaults_to_dry_run():
    args = parse("migrate-legacy", "demo")
    assert args.apply is False
    assert args.adopt is False


def test_watch_json_is_ndjson_not_pretty_document():
    args = parse("watch", "demo", "--json", "--max-polls", "1")
    assert args.json and args.max_polls == 1


def test_policy_matrix_names_are_stable():
    assert set(cli.POLICY_PROFILES) == {"interactive", "balanced", "night", "strict"}
    assert all("sensitive" in value for value in cli.POLICY_PROFILES.values())


def test_clone_copies_config_but_not_identity(tmp_path: Path):
    paths = Paths(tmp_path / "omp")
    original = Session.new(name="source", cwd=str(tmp_path), model="m", mission="mission", omp_session_id="remote")
    original.supervisor_pid = 111
    original.omp_pid = 222
    SessionStore(paths).create(original)
    rc = cli.dispatch_namespace(parse("clone", "source", "copy", "--no-install", "--json"), paths)
    assert rc == 0
    copy = SessionStore(paths).load("copy")
    assert copy.id != original.id
    assert copy.omp_session_id == ""
    assert copy.supervisor_pid == copy.omp_pid == 0
    assert copy.model == original.model and copy.mission == original.mission


def test_export_key_is_reference_not_secret_argv():
    action = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    export = action.choices["export"]
    options = {s for a in export._actions for s in a.option_strings}
    assert "--hmac-key-file" in options
    assert "--hmac-key-env" in options
    assert "--hmac-key" not in options


def test_migrate_legacy_maps_reviewed_record_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(legacy_record(tmp_path)))
    paths = Paths(tmp_path / "omp")

    rc, out, err = invoke(
        paths,
        capsys,
        "migrate-legacy",
        "migrated",
        "--source",
        str(source),
        "--no-install",
        "--json",
    )

    payload = json.loads(out)
    assert rc == 0 and err == ""
    assert payload["dry_run"] is True
    assert payload["session"]["name"] == "migrated"
    assert payload["session"]["model"] == "legacy-model"
    assert payload["session"]["omp_session_id"] == ""
    assert not (paths.sessions / "migrated.json").exists()


def test_migrate_legacy_apply_and_adopt_are_both_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(legacy_record(tmp_path)))
    paths = Paths(tmp_path / "omp")

    rc, out, _ = invoke(
        paths,
        capsys,
        "migrate-legacy",
        "migrated",
        "--source",
        str(source),
        "--apply",
        "--adopt",
        "--no-install",
        "--json",
    )

    assert rc == 0 and json.loads(out)["applied"] is True
    migrated = SessionStore(paths).load("migrated")
    assert migrated.omp_session_id == "legacy-remote-id"
    assert migrated.omp_options == ["--reasoning", "high"]
    assert migrated.restart_policy == "never"


def test_watch_emits_compact_bounded_ndjson(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )

    rc, out, err = invoke(
        paths, capsys, "watch", "demo", "--json", "--max-polls", "1"
    )

    lines = out.splitlines()
    assert rc == 0 and err == ""
    assert len(lines) == 1
    assert json.loads(lines[0])["status"]["name"] == "demo"
    assert json.loads(lines[0])["sequence"] == 1


def test_watch_suppresses_unchanged_snapshots_after_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )

    rc, out, _ = invoke(
        paths,
        capsys,
        "watch",
        "demo",
        "--json",
        "--poll-interval",
        "0",
        "--max-polls",
        "3",
    )

    assert rc == 0
    assert len(out.splitlines()) == 1


def test_watch_emits_a_new_snapshot_when_session_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    paths = Paths(tmp_path / "omp")
    store = SessionStore(paths)
    session = Session.new(name="demo", cwd=str(tmp_path), model="old", mission="mission")
    store.create(session)
    changed = False

    def change_once(_: float) -> None:
        nonlocal changed
        if not changed:
            store.patch("demo", session.id, model="new")
            changed = True

    monkeypatch.setattr(cli.time, "sleep", change_once)
    rc, out, _ = invoke(
        paths,
        capsys,
        "watch",
        "demo",
        "--json",
        "--poll-interval",
        "0",
        "--max-polls",
        "2",
    )

    snapshots = [json.loads(line) for line in out.splitlines()]
    assert rc == 0
    assert [item["status"]["model"] for item in snapshots] == ["old", "new"]
    assert [item["sequence"] for item in snapshots] == [1, 2]


def test_diagnose_writes_private_redacted_offline_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        platform="telegram",
        chat="42",
    )
    SessionStore(paths).create(session)
    outbound = Outbox(paths.outbox / "demo.json", max_attempts=1)
    outbound.enqueue("dead", {"text": "token=archive-secret"})
    outbound.fail("dead", error="password=bridge-secret")
    (paths.run / "demo.owner").write_text(
        json.dumps({"pid": 99999999, "session_id": session.id, "token": "owner-secret"})
    )
    paths.logs.mkdir(parents=True, exist_ok=True)
    (paths.logs / "demo.jsonl").write_text(
        json.dumps({"level": "error", "authorization": "Bearer report-secret"}) + "\n"
    )
    report_path = tmp_path / "diagnose.json"

    rc, out, err = invoke(
        paths,
        capsys,
        "diagnose",
        "demo",
        "--output",
        str(report_path),
        "--json",
    )

    report = json.loads(report_path.read_text())
    rendered = out + report_path.read_text()
    assert rc == 0 and err == ""
    assert report["session"]["name"] == "demo"
    assert report["state_db_used"] is False
    assert report["telegram_api_used"] is False
    assert report["events"]["count"] == 1
    assert report["logs"]["count"] == 1
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert all(secret not in rendered for secret in ("archive-secret", "bridge-secret", "owner-secret", "report-secret"))


def test_clone_copies_complete_configuration_but_no_runtime_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    source = Session.new(
        name="source",
        cwd=str(tmp_path),
        project="project",
        model="m",
        mission="mission",
        platform="telegram",
        chat="42",
        topic="7",
        allowed_users=["9"],
        restart_policy="never",
        omp_options=["--reasoning", "high"],
        omp_session_id="remote",
        policy_profile="night",
    )
    SessionStore(paths).create(source)
    (paths.run / "source.omp-path").write_text("/bin/true\n")
    (paths.run / "source.runtime.json").write_text('{"seen_event_ids":["secret"]}\n')
    Outbox(paths.outbox / "source.json").enqueue("event", {"text": "queued"})

    rc, out, _ = invoke(
        paths, capsys, "clone", "source", "copy", "--no-install", "--json"
    )

    clone = SessionStore(paths).load("copy")
    assert rc == 0 and json.loads(out)["cloned"] == "copy"
    assert clone.policy_profile == "night"
    assert clone.project == "project"
    assert clone.omp_options == ["--reasoning", "high"]
    assert clone.allowed_users == ["9"]
    assert clone.restart_policy == "never"
    assert (paths.run / "copy.omp-path").read_text() == "/bin/true\n"
    assert not (paths.run / "copy.runtime.json").exists()
    assert not (paths.outbox / "copy.json").exists()


def test_policy_selection_persists_across_create_and_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    rc, _, _ = invoke(
        paths,
        capsys,
        "create",
        "demo",
        "--cwd",
        str(tmp_path),
        "--model",
        "m",
        "--mission",
        "mission",
        "--policy",
        "night",
        "--omp-path",
        "/bin/true",
        "--no-install",
        "--json",
    )
    assert rc == 0 and SessionStore(paths).load("demo").policy_profile == "night"

    rc, _, _ = invoke(
        paths,
        capsys,
        "update",
        "demo",
        "--policy",
        "strict",
        "--no-install",
        "--json",
    )
    assert rc == 0 and SessionStore(paths).load("demo").policy_profile == "strict"


@pytest.mark.parametrize(
    ("profile", "automatic"),
    [("interactive", False), ("balanced", True), ("night", True), ("strict", False)],
)
def test_policy_profiles_control_only_safe_automatic_answers(
    tmp_path: Path, profile: str, automatic: bool
):
    paths = Paths(tmp_path / profile / "omp")
    session = Session.new(
        name=profile,
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        policy_profile=profile,
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true")
    action = runtime.on_event(
        {
            "type": "extension_ui_request",
            "id": "safe",
            "title": "Continue?",
            "options": [{"label": "Continue", "recommended": True, "reversible": True}],
        }
    )
    assert bool(action and action.get("rpc")) is automatic

    sensitive_paths = Paths(tmp_path / f"{profile}-sensitive" / "omp")
    sensitive_session = Session.new(
        name=f"{profile}-sensitive",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        policy_profile=profile,
    )
    SessionStore(sensitive_paths).create(sensitive_session)
    sensitive_runtime = Runtime(sensitive_session, sensitive_paths, omp_path="/bin/true")
    sensitive = sensitive_runtime.on_event(
        {
            "type": "extension_ui_request",
            "id": "risky",
            "title": "Publish release?",
            "options": [{"label": "Publish", "recommended": True, "reversible": True}],
        }
    )
    assert not (sensitive and sensitive.get("rpc"))


def test_export_hmac_key_file_signs_and_import_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    key = tmp_path / "archive.key"
    key.write_bytes(b"file-key-secret")
    archive = tmp_path / "archive.json"

    rc, out, _ = invoke(
        paths,
        capsys,
        "export",
        "demo",
        str(archive),
        "--hmac-key-file",
        str(key),
        "--json",
    )
    exported = json.loads(archive.read_text())
    assert rc == 0 and "file-key-secret" not in out + archive.read_text()
    assert exported["integrity"]["algorithm"] == "hmac-sha256"
    assert len(exported["integrity"]["digest"]) == 64

    rc, out, _ = invoke(
        paths,
        capsys,
        "import",
        str(archive),
        "--conflict",
        "rename",
        "--hmac-key-file",
        str(key),
        "--no-install",
        "--json",
    )
    assert rc == 0 and json.loads(out)["imported"] == "demo-2"


def test_export_hmac_env_reference_never_serializes_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    monkeypatch.setenv("HERMES_OMP_TEST_ARCHIVE_KEY", "environment-key-secret")
    archive = tmp_path / "archive.json"

    rc, out, _ = invoke(
        paths,
        capsys,
        "export",
        "demo",
        str(archive),
        "--hmac-key-env",
        "HERMES_OMP_TEST_ARCHIVE_KEY",
        "--json",
    )

    assert rc == 0
    assert "environment-key-secret" not in out + archive.read_text()


def test_signed_import_rejects_tampering_before_state_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    key = tmp_path / "archive.key"
    key.write_bytes(b"tamper-test-key")
    archive = tmp_path / "archive.json"
    assert invoke(
        paths,
        capsys,
        "export",
        "demo",
        str(archive),
        "--hmac-key-file",
        str(key),
        "--json",
    )[0] == 0
    value = json.loads(archive.read_text())
    value["session"]["mission"] = "tampered"
    archive.write_text(json.dumps(value))

    rc, out, _ = invoke(
        paths,
        capsys,
        "import",
        str(archive),
        "--conflict",
        "rename",
        "--hmac-key-file",
        str(key),
        "--no-install",
        "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"
    assert not (paths.sessions / "demo-2.json").exists()


def test_import_can_require_a_signed_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    archive = tmp_path / "archive.json"
    assert invoke(paths, capsys, "export", "demo", str(archive), "--json")[0] == 0

    rc, out, _ = invoke(
        paths,
        capsys,
        "import",
        str(archive),
        "--conflict",
        "rename",
        "--require-signature",
        "--no-install",
        "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert "signature" in json.loads(out)["error"]["message"].lower()
    assert not (paths.sessions / "demo-2.json").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "run/copy.runtime.json",
        "run/copy.question.json",
        "run/copy.owner",
        "run/copy.prompts.json",
        "outbox/copy.json",
        "inbox/copy/stale.json",
        "logs/copy.jsonl",
    ],
)
def test_clone_rejects_every_residual_destination_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    relative: str,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="source", cwd=str(tmp_path), model="m", mission="mission")
    )
    residual = paths.root / relative
    residual.parent.mkdir(parents=True, exist_ok=True)
    residual.write_text("[]\n")

    rc, out, _ = invoke(
        paths, capsys, "clone", "source", "copy", "--no-install", "--json"
    )

    assert rc == cli.EXIT_CONFLICT
    assert json.loads(out)["error"]["code"] == "conflict"
    assert residual.read_text() == "[]\n"
    assert not (paths.sessions / "copy.json").exists()
    assert not (paths.run / "copy.omp-path").exists()


def test_redaction_masks_values_following_secret_argv_flags():
    rendered = json.dumps(
        redact(
            {
                "argv": [
                    "omp",
                    "--password",
                    "separate-password",
                    "--token=inline-token",
                    "safe",
                ]
            }
        )
    )

    assert "separate-password" not in rendered
    assert "inline-token" not in rendered
    assert "safe" in rendered


def test_default_legacy_discovery_rejects_symlink_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    profile = tmp_path / "profile"
    profile.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(legacy_record(tmp_path)))
    (profile / "omp-legacy.json").symlink_to(outside)
    paths = Paths(profile / "omp")

    rc, out, _ = invoke(paths, capsys, "migrate-legacy", "escaped", "--json")

    assert rc == cli.EXIT_NOT_FOUND
    assert json.loads(out)["error"]["code"] == "not_found"
    assert not (paths.sessions / "escaped.json").exists()


def test_clone_dry_run_does_not_migrate_or_create_profile_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    paths.sessions.mkdir(parents=True)
    source = paths.sessions / "source.json"
    original = json.dumps(
        {
            "schema_version": 1,
            "name": "source",
            "cwd": str(tmp_path),
            "model": "m",
            "mission": "mission",
        }
    ).encode()
    source.write_bytes(original)

    rc, out, _ = invoke(
        paths,
        capsys,
        "clone",
        "source",
        "copy",
        "--dry-run",
        "--no-install",
        "--json",
    )

    assert rc == 0 and json.loads(out)["dry_run"] is True
    assert source.read_bytes() == original
    assert {item.name for item in paths.sessions.iterdir()} == {"source.json"}
    assert not paths.run.exists()
    assert not paths.logs.exists()
    assert not paths.outbox.exists()
    assert not paths.inbox.exists()


def test_watch_flushes_each_stream_record(tmp_path: Path):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )

    class FlushTrackingStream(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    stream = FlushTrackingStream()
    with contextlib.redirect_stdout(stream):
        rc = cli.dispatch_namespace(
            parse("watch", "demo", "--json", "--max-polls", "1"), paths
        )

    assert rc == 0
    assert stream.flushes == 1
    assert len(stream.getvalue().splitlines()) == 1


@pytest.mark.parametrize("interval", ["nan", "inf"])
def test_watch_rejects_non_finite_intervals_before_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    interval: str,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )

    rc, out, _ = invoke(
        paths,
        capsys,
        "watch",
        "demo",
        "--json",
        "--poll-interval",
        interval,
        "--max-polls",
        "1",
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"


def test_signed_import_rejects_non_ascii_key_id_as_validation_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    key = tmp_path / "archive.key"
    key.write_bytes(b"fingerprint-test-key")
    archive = tmp_path / "archive.json"
    assert invoke(
        paths,
        capsys,
        "export",
        "demo",
        str(archive),
        "--hmac-key-file",
        str(key),
        "--json",
    )[0] == 0
    value = json.loads(archive.read_text())
    value["integrity"]["key_id"] = "é"
    archive.write_text(json.dumps(value))

    rc, out, _ = invoke(
        paths,
        capsys,
        "import",
        str(archive),
        "--hmac-key-file",
        str(key),
        "--no-install",
        "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"


def test_diagnose_output_oserror_uses_stable_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )

    def fail_write(*_args, **_kwargs):
        raise OSError("private-path-must-not-leak")

    monkeypatch.setattr(cli, "atomic_write", fail_write)
    rc, out, err = invoke(
        paths,
        capsys,
        "diagnose",
        "demo",
        "--output",
        str(tmp_path / "diagnosis.json"),
        "--json",
    )

    assert rc == cli.EXIT_ERROR and err == ""
    payload = json.loads(out)
    assert payload["error"]["code"] == "error"
    assert "private-path-must-not-leak" not in out


@pytest.mark.parametrize("profile", ["balanced", "night"])
@pytest.mark.parametrize(
    "title",
    [
        "Grant permissions?",
        "Share credentials?",
        "Run a privileged command?",
        "Authorize access?",
    ],
)
def test_sensitive_language_is_never_automatically_answered(
    tmp_path: Path,
    profile: str,
    title: str,
):
    paths = Paths(tmp_path / profile / str(abs(hash(title))) / "omp")
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        policy_profile=profile,
    )
    SessionStore(paths).create(session)
    runtime = Runtime(session, paths, omp_path="/bin/true")

    action = runtime.on_event(
        {
            "type": "extension_ui_request",
            "id": "sensitive",
            "title": title,
            "options": [
                {"label": "Continue", "recommended": True, "reversible": True}
            ],
        }
    )

    assert not (action and action.get("rpc"))


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        (("watch", "missing", "--json", "--max-polls", "1"), cli.EXIT_NOT_FOUND),
        (("watch", "missing", "--json", "--poll-interval", "nan"), cli.EXIT_VALIDATION),
    ],
)
def test_watch_json_errors_are_single_flushed_ndjson_records(
    tmp_path: Path,
    argv: tuple[str, ...],
    expected_code: int,
):
    paths = Paths(tmp_path / "omp")

    class FlushTrackingStream(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    stream = FlushTrackingStream()
    with contextlib.redirect_stdout(stream):
        rc = cli.dispatch_namespace(parse(*argv), paths)

    lines = stream.getvalue().splitlines()
    assert rc == expected_code
    assert len(lines) == 1
    assert json.loads(lines[0])["ok"] is False
    assert stream.flushes == 1


def test_migrate_legacy_adopt_requires_recorded_resume_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    record = legacy_record(tmp_path)
    del record["omp_session_id"]
    source = tmp_path / "legacy-without-resume.json"
    source.write_text(json.dumps(record))
    paths = Paths(tmp_path / "omp")

    rc, out, _ = invoke(
        paths,
        capsys,
        "migrate-legacy",
        "migrated",
        "--source",
        str(source),
        "--adopt",
        "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert "resume" in json.loads(out)["error"]["message"].lower()
    assert not (paths.sessions / "migrated.json").exists()


def test_clone_revalidates_source_configuration_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    project = tmp_path / "project"
    project.mkdir()
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="source", cwd=str(project), model="m", mission="mission")
    )
    project.rmdir()

    rc, out, _ = invoke(
        paths, capsys, "clone", "source", "copy", "--no-install", "--json"
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"
    assert not (paths.sessions / "copy.json").exists()


def test_diagnose_tolerates_inbox_event_moved_during_collection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    event = paths.inbox / "demo" / "event.json"
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({"event_id": "event"}))
    original_read_text = Path.read_text

    def moving_read(path: Path, *args, **kwargs):
        value = original_read_text(path, *args, **kwargs)
        if path == event:
            path.unlink()
        return value

    monkeypatch.setattr(Path, "read_text", moving_read)
    rc, out, err = invoke(paths, capsys, "diagnose", "demo", "--json")

    assert rc == 0 and err == ""
    assert json.loads(out)["session"]["name"] == "demo"


@pytest.mark.parametrize("option", ["--hmac-key-file", "--hmac-key-env"])
def test_export_rejects_explicit_empty_hmac_key_reference(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
):
    paths = Paths(tmp_path / "omp")
    SessionStore(paths).create(
        Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission")
    )
    archive = tmp_path / "archive.json"

    rc, out, _ = invoke(
        paths,
        capsys,
        "export",
        "demo",
        str(archive),
        option,
        "",
        "--json",
    )

    assert rc == cli.EXIT_VALIDATION
    assert json.loads(out)["error"]["code"] == "validation"
    assert not archive.exists()
