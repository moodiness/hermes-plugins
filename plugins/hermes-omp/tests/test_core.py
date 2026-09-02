from __future__ import annotations

import json
import os
import subprocess
import stat
import sys
import time
import threading
from pathlib import Path

import pytest
import hermes_omp.cli as cli
import hermes_omp.core as core

from hermes_omp.core import (
    Authorization,
    Outbox,
    Paths,
    Question,
    Session,
    SessionStore,
    classify_safe_answer,
    parse_rpc_line,
    redact,
)


def test_paths_are_profile_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert Paths.discover().root == tmp_path / "omp"


def test_state_round_trip_is_atomic_private_and_complete(tmp_path: Path) -> None:
    store = SessionStore(Paths(tmp_path / "omp"))
    session = Session.new(
        name="demo", cwd=str(tmp_path), model="gpt-5.6-sol-pro", mission="ship",
        platform="telegram", chat="42", topic="7", restart_policy="on-failure",
        omp_session_id="omp-123", plugin_version="0.1.0rc1",
        hermes_version="0.20.6", omp_version="18.0.10",
    )
    store.save(session)
    loaded = store.load("demo")
    assert loaded == session
    assert loaded.schema_version == 2
    if os.name != "nt":
        assert stat.S_IMODE((store.paths.sessions / "demo.json").stat().st_mode) == 0o600
    assert not list(store.paths.sessions.glob("*.tmp"))


def test_atomic_write_chmod_failure_preserves_old_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old")
    monkeypatch.setattr(core.os, "chmod", lambda *args: (_ for _ in ()).throw(OSError("chmod failed")))

    with pytest.raises(OSError, match="chmod failed"):
        core.atomic_write(path, "new")

    assert path.read_text() == "old"


def test_migrates_v1_and_quarantines_partial_json(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    store = SessionStore(paths)
    paths.ensure()
    old = paths.sessions / "old.json"
    old.write_text(json.dumps({"schema_version": 1, "name": "old", "cwd": str(tmp_path), "model": "m", "mission": "x"}))
    migrated = store.load("old")
    assert migrated.schema_version == 2
    assert migrated.restart_policy == "on-failure"
    broken = paths.sessions / "broken.json"
    broken.write_text("{")
    with pytest.raises(ValueError, match="quarantined"):
        store.load("broken")
    assert list(paths.quarantine.glob("broken.*.json"))


def test_session_patch_merges_owned_fields_and_rejects_stale_identity(tmp_path: Path) -> None:
    store = SessionStore(Paths(tmp_path / "omp"))
    session = Session.new(name="demo", cwd=str(tmp_path), model="old", mission="x")
    store.save(session)

    current = store.load("demo")
    current.model = "new"
    store.save(current)
    patched = store.patch("demo", session.id, last_activity=123.0, status="running")
    assert patched.model == "new"
    assert (patched.last_activity, patched.status) == (123.0, "running")

    replacement = Session.new(name="demo", cwd=str(tmp_path), model="replacement", mission="y")
    store.save(replacement)
    with pytest.raises(ValueError, match="identity changed"):
        store.patch("demo", session.id, last_activity=456.0)
    assert store.load("demo") == replacement

    (store.paths.sessions / "demo.json").unlink()
    with pytest.raises(FileNotFoundError):
        store.patch("demo", replacement.id, status="crashed")
    assert not (store.paths.sessions / "demo.json").exists()


def test_blocked_stale_patch_cannot_overwrite_replacement(tmp_path: Path) -> None:
    store = SessionStore(Paths(tmp_path / "omp"))
    original = Session.new(name="demo", cwd=str(tmp_path), model="old", mission="x")
    replacement = Session.new(name="demo", cwd=str(tmp_path), model="replacement", mission="y")
    store.save(original)
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def stale_patch() -> None:
        started.set()
        try:
            store.patch("demo", original.id, last_activity=999.0)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with store.transaction():
        worker = threading.Thread(target=stale_patch)
        worker.start()
        assert started.wait(1) and not finished.wait(0.1)
        store.replace(replacement)
    worker.join(1)

    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert store.load("demo") == replacement


def test_redacts_common_secrets_recursively() -> None:
    value = {"authorization": "Bearer abc.DEF-123", "url": "https://api.example/?token=secret", "nested": ["password=hunter2", "safe"]}
    text = json.dumps(redact(value))
    assert "abc.DEF-123" not in text and "secret" not in text and "hunter2" not in text
    assert "safe" in text and "[REDACTED]" in text


def test_rpc_parser_preserves_question_options_and_rejects_garbage() -> None:
    raw = json.dumps({"type": "extension_ui_request", "id": "q-1", "method": "select", "title": "Choose", "options": [{"label": "A", "description": "Alpha", "recommended": True, "reversible": True}]})
    event = parse_rpc_line(raw)
    question = Question.from_event(event, session_name="demo", ttl=60, now=100)
    assert question.id == "q-1"
    assert question.options[0].description == "Alpha"
    assert question.expires_at == 160
    with pytest.raises(ValueError):
        parse_rpc_line("not-json")


def test_authorization_requires_exact_route_user_and_question() -> None:
    auth = Authorization(platform="telegram", chat="42", topic="7", users=("9",))
    good = {"platform": "telegram", "chat": "42", "topic": "7", "user": "9", "question_id": "q1", "event_id": "e1", "answer": "1"}
    assert auth.authorize(good, expected_question_id="q1", seen_event_ids=set())
    for changed in ({"topic": "8"}, {"user": "10"}, {"question_id": "q2"}):
        bad = {**good, **changed}
        assert not auth.authorize(bad, expected_question_id="q1", seen_event_ids=set())
    assert not auth.authorize(good, expected_question_id="q1", seen_event_ids={"e1"})


def test_safe_auto_answer_is_recommended_reversible_and_not_risky() -> None:
    safe = Question.from_event({"id": "q", "title": "Continue?", "options": [{"label": "Continue", "recommended": True, "reversible": True}]}, "demo", 60, 0)
    assert classify_safe_answer(safe) == "Continue"
    risky = Question.from_event({"id": "q", "title": "Push release?", "options": [{"label": "Push", "recommended": True, "reversible": True}]}, "demo", 60, 0)
    assert classify_safe_answer(risky) is None
    irreversible = Question.from_event({"id": "q", "title": "Continue?", "options": [{"label": "Continue", "recommended": True, "reversible": False}]}, "demo", 60, 0)
    assert classify_safe_answer(irreversible) is None


def test_outbox_deduplicates_orders_retries_and_dead_letters(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.json", max_attempts=2, base_delay=2, jitter=lambda: 0)
    assert outbox.enqueue("a", {"text": "one"})
    assert not outbox.enqueue("a", {"text": "duplicate"})
    assert outbox.enqueue("b", {"text": "two"})
    assert [x.id for x in outbox.due(now=0)] == ["a"]
    outbox.fail("a", now=10, error="offline")
    assert outbox.due(now=11) == []
    outbox.fail("a", now=12, error="offline")
    assert outbox.dead_letters()[0].id == "a"
    outbox.ack("b")
    assert outbox.pending() == []
    reloaded = Outbox(tmp_path / "outbox.json")
    assert reloaded.dead_letters()[0].id == "a"


def test_stale_writers_preserve_fifo_and_unrelated_items(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    first = Outbox(path)
    second = Outbox(path)

    assert first.enqueue("a", {"n": 1})
    assert second.enqueue("b", {"n": 2})
    assert [item.id for item in Outbox(path).items] == ["a", "b"]

    first.ack("a")
    assert [(item.id, item.state) for item in Outbox(path).items] == [
        ("a", "delivered"),
        ("b", "pending"),
    ]


def test_stale_fail_and_retry_preserve_concurrent_items(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    seed = Outbox(path, max_attempts=1)
    seed.enqueue("dead", {"n": 1})
    stale_fail = Outbox(path, max_attempts=1)
    stale_retry = Outbox(path, max_attempts=1)
    concurrent = Outbox(path, max_attempts=1)

    concurrent.enqueue("other", {"n": 2})
    stale_fail.fail("dead", error="offline")
    assert [(item.id, item.state) for item in Outbox(path).items] == [
        ("dead", "dead"),
        ("other", "pending"),
    ]

    Outbox(path).enqueue("later", {"n": 3})
    assert stale_retry.retry("dead") == ["dead"]
    assert [(item.id, item.state) for item in Outbox(path).items] == [
        ("dead", "pending"),
        ("other", "pending"),
        ("later", "pending"),
    ]


def test_stale_reader_refreshes_every_public_view(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    reader = Outbox(path)
    writer = Outbox(path, max_attempts=1)
    writer.enqueue("pending", {"n": 1})
    writer.enqueue("dead", {"n": 2})
    writer.fail("dead", error="offline")

    assert [item.id for item in reader.items] == ["pending", "dead"]
    assert [item.id for item in reader.pending()] == ["pending"]
    assert [item.id for item in reader.due(now=time.time())] == ["pending"]
    assert [item.id for item in reader.dead_letters()] == ["dead"]


def test_stale_writer_does_not_overwrite_malformed_queue(tmp_path: Path) -> None:
    path = tmp_path / "outbox.json"
    writer = Outbox(path)
    path.write_bytes(b"{not-json")

    with pytest.raises(json.JSONDecodeError):
        writer.enqueue("new", {"n": 1})

    assert path.read_bytes() == b"{not-json"


_PATH_LOCK_WORKER = """
import sys
import time
from pathlib import Path

from hermes_omp import core

target = Path(sys.argv[1])
acquired = Path(sys.argv[2])
release = sys.argv[3]
lock_timeout = sys.argv[4]
if lock_timeout != "-":
    core._LOCK_TIMEOUT_SECONDS = float(lock_timeout)
acquired.with_name(acquired.name + ".attempting").write_text(
    "attempting\\n", encoding="utf-8"
)
try:
    with core._path_lock(target):
        acquired.write_text("acquired\\n", encoding="utf-8")
        if release != "-":
            deadline = time.monotonic() + 15
            release_path = Path(release)
            while not release_path.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for test release")
                time.sleep(0.01)
except TimeoutError as error:
    if not str(error).startswith("timed out waiting for lock:"):
        raise
    acquired.with_name(acquired.name + ".timed-out").write_text(
        "timed out\\n", encoding="utf-8"
    )
"""


def _spawn_path_lock_worker(
    path: Path,
    acquired: Path,
    release: Path | None = None,
    *,
    module_root: Path | None = None,
    lock_timeout: float | None = None,
) -> subprocess.Popen[str]:
    env = dict(os.environ)
    if module_root is not None:
        env["PYTHONPATH"] = str(module_root)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PATH_LOCK_WORKER,
            str(path),
            str(acquired),
            "-" if release is None else str(release),
            "-" if lock_timeout is None else str(lock_timeout),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_path(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()

def _assert_worker_reached(
    process: subprocess.Popen[str], path: Path, message: str, timeout: float = 5.0
) -> None:
    if _wait_for_path(path, timeout):
        return
    if process.poll() is None:
        process.kill()
    stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(
        f"{message}; returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
    )


def _assert_process_ok(process: subprocess.Popen[str], timeout: float = 5.0) -> None:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"subprocess did not exit; stdout={stdout!r}, stderr={stderr!r}"
        ) from error
    assert process.returncode == 0, stderr or stdout


def _cleanup_processes(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
    for process in processes:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_path_lock_excludes_other_processes_without_replacing_guard(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.json"
    lock_path = path.with_name("queue.json.lock")
    first_ready = tmp_path / "first-ready"
    blocked_ready = tmp_path / "blocked-ready"
    after_release_ready = tmp_path / "after-release-ready"
    release_first = tmp_path / "release-first"
    processes: list[subprocess.Popen[str]] = []
    try:
        first = _spawn_path_lock_worker(path, first_ready, release_first)
        processes.append(first)
        _assert_worker_reached(
            first, first_ready, "first subprocess never acquired the lock"
        )
        assert lock_path.is_file()
        guard = lock_path.stat()

        blocked = _spawn_path_lock_worker(
            path, blocked_ready, lock_timeout=0.1
        )
        processes.append(blocked)
        _assert_worker_reached(
            blocked,
            blocked_ready.with_name(blocked_ready.name + ".timed-out"),
            "contending subprocess did not time out",
        )
        _assert_process_ok(blocked)
        assert not blocked_ready.exists()
        assert first.poll() is None

        release_first.touch()
        _assert_process_ok(first)
        after_release = _spawn_path_lock_worker(path, after_release_ready)
        processes.append(after_release)
        _assert_worker_reached(
            after_release,
            after_release_ready,
            "subprocess never acquired the released lock",
        )
        _assert_process_ok(after_release)
    finally:
        _cleanup_processes(processes)

    assert lock_path.is_file()
    assert os.path.samestat(guard, lock_path.stat())


def test_path_lock_recovers_after_owner_process_crashes(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    first_ready = tmp_path / "first-ready"
    never_release = tmp_path / "never-release"
    recovered = tmp_path / "recovered"
    processes: list[subprocess.Popen[str]] = []
    try:
        first = _spawn_path_lock_worker(path, first_ready, never_release)
        processes.append(first)
        _assert_worker_reached(
            first, first_ready, "crashing subprocess never acquired the lock"
        )
        guard = path.with_name("queue.json.lock").stat()
        first.kill()
        first.communicate(timeout=5)
        assert first.returncode != 0

        second = _spawn_path_lock_worker(path, recovered)
        processes.append(second)
        _assert_worker_reached(
            second, recovered, "dead owner lock was not recovered"
        )
        _assert_process_ok(second)
    finally:
        _cleanup_processes(processes)
    assert os.path.samestat(guard, path.with_name("queue.json.lock").stat())

    assert path.with_name("queue.json.lock").is_file()


def test_doctor_migrates_abandoned_legacy_path_lock(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    paths.ensure()
    path = paths.run / "queue.json"
    lock_path = path.with_name("queue.json.lock")
    dead_owner = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _assert_process_ok(dead_owner)
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "pid": dead_owner.pid,
                "created_at": time.time(),
                "token": "legacy",
            }
        ),
        encoding="utf-8",
    )

    preview = cli.doctor(paths)
    preview_repair = next(
        item
        for item in preview["repairs"]
        if item["action"] == "migrate_legacy_path_lock"
    )
    assert preview_repair == {
        "action": "migrate_legacy_path_lock",
        "path": str(lock_path),
        "applied": False,
    }
    assert lock_path.is_dir()

    report = cli.doctor(paths, fix=True)

    repair = next(
        item
        for item in report["repairs"]
        if item["action"] == "migrate_legacy_path_lock"
    )
    assert repair == {
        "action": "migrate_legacy_path_lock",
        "path": str(lock_path),
        "applied": True,
    }
    with core._path_lock(path):
        assert lock_path.is_file()


def test_doctor_does_not_disturb_live_legacy_path_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "omp")
    paths.ensure()
    path = paths.run / "queue.json"
    lock_path = path.with_name("queue.json.lock")
    live_owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(15)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "pid": live_owner.pid,
                "created_at": time.time(),
                "token": "legacy",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        report = cli.doctor(paths, fix=True)
        repair = next(
            item
            for item in report["repairs"]
            if item["action"] == "migrate_legacy_path_lock"
        )
        assert repair == {
            "action": "migrate_legacy_path_lock",
            "path": str(lock_path),
            "applied": False,
            "reason": "live_or_unverifiable_owner",
        }
        with pytest.raises(TimeoutError, match="timed out waiting for lock"):
            with core._path_lock(path):
                pass
        assert live_owner.poll() is None
    finally:
        _cleanup_processes([live_owner])

    assert lock_path.is_dir()


def test_doctor_requires_every_session_stopped_before_legacy_migration(
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "omp")
    paths.ensure()
    lock_path = paths.sessions / ".store.lock"
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps(
            {
                "pid": 99999999,
                "created_at": time.time(),
                "token": "legacy",
            }
        ),
        encoding="utf-8",
    )
    live_owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(15)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    (paths.run / "active.owner").write_text(
        json.dumps({"pid": live_owner.pid, "token": "active"}),
        encoding="utf-8",
    )
    try:
        report = cli.doctor(paths, fix=True)
        repair = next(
            item
            for item in report["repairs"]
            if item["action"] == "migrate_legacy_path_lock"
        )
        assert repair["applied"] is False
        assert repair["reason"] == "live_writer"
        assert lock_path.is_dir()
    finally:
        _cleanup_processes([live_owner])


def test_path_lock_times_out_without_disturbing_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.json"
    first_ready = tmp_path / "first-ready"
    release_first = tmp_path / "release-first"
    first = _spawn_path_lock_worker(path, first_ready, release_first)
    try:
        _assert_worker_reached(first, first_ready, "subprocess never acquired the lock")
        monkeypatch.setattr(core, "_LOCK_TIMEOUT_SECONDS", 0.05)

        with pytest.raises(TimeoutError, match="timed out waiting for lock"):
            with core._path_lock(path):
                pass

        assert first.poll() is None
        release_first.touch()
        _assert_process_ok(first)
    finally:
        _cleanup_processes([first])


def test_source_and_vendored_path_locks_exclude_each_other(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    source_ready = tmp_path / "source-ready"
    blocked_ready = tmp_path / "vendored-blocked-ready"
    after_release_ready = tmp_path / "vendored-after-release-ready"
    release_source = tmp_path / "release-source"
    plugin_root = Path(__file__).parents[1] / "plugin"
    processes: list[subprocess.Popen[str]] = []
    try:
        source = _spawn_path_lock_worker(path, source_ready, release_source)
        processes.append(source)
        _assert_worker_reached(
            source, source_ready, "source subprocess never acquired the lock"
        )

        blocked = _spawn_path_lock_worker(
            path,
            blocked_ready,
            module_root=plugin_root,
            lock_timeout=0.1,
        )
        processes.append(blocked)
        _assert_worker_reached(
            blocked,
            blocked_ready.with_name(blocked_ready.name + ".timed-out"),
            "vendored subprocess did not time out on the source lock",
        )
        _assert_process_ok(blocked)
        assert not blocked_ready.exists()
        assert source.poll() is None

        release_source.touch()
        _assert_process_ok(source)
        after_release = _spawn_path_lock_worker(
            path, after_release_ready, module_root=plugin_root
        )
        processes.append(after_release)
        _assert_worker_reached(
            after_release,
            after_release_ready,
            "vendored subprocess never acquired the released lock",
        )
        _assert_process_ok(after_release)
    finally:
        _cleanup_processes(processes)


def test_path_lock_is_reentrant_and_remains_usable_after_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.json"

    with pytest.raises(RuntimeError, match="inside lock"):
        with core._path_lock(path):
            with core._path_lock(path):
                raise RuntimeError("inside lock")

    with core._path_lock(path):
        pass

    assert path.with_name("queue.json.lock").is_file()
