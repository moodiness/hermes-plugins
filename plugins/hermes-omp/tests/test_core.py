from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
import sys
from types import SimpleNamespace

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
    assert stat.S_IMODE((store.paths.sessions / "demo.json").stat().st_mode) == 0o600
    assert not list(store.paths.sessions.glob("*.tmp"))


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


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_path_lock_uses_platform_lock_and_releases_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    calls: list[tuple[object, ...]] = []
    if platform == "posix":
        manager = SimpleNamespace(
            LOCK_EX="exclusive",
            LOCK_UN="unlock",
            flock=lambda fd, mode: calls.append((fd, mode)),
        )
        monkeypatch.setitem(sys.modules, "fcntl", manager)
    else:
        manager = SimpleNamespace(
            LK_LOCK="exclusive",
            LK_UNLCK="unlock",
            locking=lambda fd, mode, size: calls.append((fd, mode, size)),
        )
        monkeypatch.setitem(sys.modules, "msvcrt", manager)
    monkeypatch.setattr(core.os, "name", platform)
    path = tmp_path / "queue.json"

    with pytest.raises(RuntimeError, match="inside lock"):
        with core._path_lock(path):
            with core._path_lock(path):
                assert path.with_name("queue.json.lock").read_bytes() == b"\0"
                raise RuntimeError("inside lock")

    modes = [call[1] for call in calls]
    assert modes == ["exclusive", "unlock"]


def test_path_lock_setup_failure_does_not_skip_next_platform_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    manager = SimpleNamespace(
        LOCK_EX="exclusive",
        LOCK_UN="unlock",
        flock=lambda fd, mode: calls.append(mode),
    )
    monkeypatch.setitem(sys.modules, "fcntl", manager)
    monkeypatch.setattr(core.os, "name", "posix")
    path = tmp_path / "queue.json"
    lock_path = path.with_name("queue.json.lock")
    original_open = Path.open
    failed = False

    def fail_first_open(self, *args, **kwargs):
        nonlocal failed
        if self == lock_path and not failed:
            failed = True
            raise OSError("injected lock open failure")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_first_open)

    with pytest.raises(OSError, match="injected lock open failure"):
        with core._path_lock(path):
            pass
    with core._path_lock(path):
        pass

    assert calls == ["exclusive", "unlock"]
