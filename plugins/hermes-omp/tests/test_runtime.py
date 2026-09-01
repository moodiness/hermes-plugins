from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.bridge import HermesSendBridge
from hermes_omp.runtime import RpcLineBuffer, Runtime, acquire_owner_lock, build_omp_command, inspect_adoption, run

def _write_transactional_fake(tmp_path: Path) -> Path:
    fake = tmp_path / "transactional_fake_omp.py"
    fake.write_text(
        """import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

done = Path(os.environ["FAKE_OMP_DONE"])

def finish(code=0):
    done.write_text(str(code))
    raise SystemExit(code)

def watchdog():
    time.sleep(4)
    done.write_text("watchdog")
    os._exit(4)

threading.Thread(target=watchdog, daemon=True).start()
def close_stdin():
    if os.name == "nt":
        import ctypes
        import msvcrt
        ctypes.windll.kernel32.CloseHandle(msvcrt.get_osfhandle(sys.stdin.fileno()))
    else:
        os.close(sys.stdin.fileno())

mode = os.environ["FAKE_OMP_MODE"]
for raw in sys.stdin.buffer:
    frame = json.loads(raw)
    if frame.get("type") == "prompt" and frame.get("id") == "initial":
        question = {"type":"extension_ui_request","id":"q","method":"select","title":"Pick","options":[{"label":"A"}]}
        if mode == "failure":
            close_stdin()
            time.sleep(0.05)
            print(json.dumps(question), flush=True)
            time.sleep(0.5)
            finish()
        if mode == "residue":
            question["token"] = "top-secret"
            sys.stdout.buffer.write(json.dumps(question).encode())
            sys.stdout.buffer.flush()
            finish()
        if mode == "inherited_stdout":
            code = """ + repr(
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "print(json.dumps({'type':'turn_end','content':'descendant-output'}), flush=True)\n"
                "time.sleep(1.5)\n"
                "Path(os.environ['FAKE_DESC_DONE']).write_text('done')\n"
            ) + """
            subprocess.Popen([sys.executable, "-c", code], stdout=sys.stdout, stderr=sys.stderr)
            finish()
        print(json.dumps(question), flush=True)
    elif frame.get("type") == "extension_ui_response":
        Path(os.environ["FAKE_OMP_OBSERVED"]).write_bytes(raw)
        finish()
finish()
""",
        encoding="utf-8",
    )
    return fake


def _transactional_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[Paths, list[subprocess.Popen[str]], Path]:
    paths = Paths(tmp_path / "omp")
    fake = _write_transactional_fake(tmp_path)
    session = Session.new(
        name="demo",
        cwd=str(tmp_path),
        model="m",
        mission="mission",
        platform="telegram",
        chat="42",
        topic="7",
        allowed_users=["9"],
        omp_options=[str(fake)],
    )
    SessionStore(paths).save(session)
    observed = tmp_path / "observed.jsonl"
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda *_: None)
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_MODE", mode)
    monkeypatch.setenv("FAKE_OMP_DONE", str(tmp_path / "fake.done"))
    monkeypatch.setenv("FAKE_OMP_OBSERVED", str(observed))
    monkeypatch.setenv("FAKE_DESC_DONE", str(tmp_path / "descendant.done"))
    inbox = paths.inbox / "demo"
    inbox.mkdir(parents=True, exist_ok=True)
    event = {"event_id":"e","question_id":"q","platform":"telegram","chat":"42","topic":"7","user":"9","answer":"1"}
    (inbox / "e.json").write_text(json.dumps(event), encoding="utf-8")
    return paths, children, observed


def _stop_fakes(children: list[subprocess.Popen[str]]) -> None:
    for child in children:
        try:
            child.wait(timeout=0.5)
            continue
        except subprocess.TimeoutExpired:
            child.terminate()
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=0.5)



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


def test_runtime_prepares_inbound_until_response_is_committed(tmp_path: Path) -> None:
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
    assert accepted.question_id == "q1"
    assert runtime.question is not None
    assert "e1" not in runtime.seen
    assert runtime.session.last_activity == 1
    reloaded = Runtime(store.load("demo"), paths, omp_path="fake")
    assert reloaded.question is not None and "e1" not in reloaded.seen
    runtime.commit_response(accepted.question_id, "e1", now=3)
    assert runtime.question is None
    assert "e1" in runtime.seen
    assert runtime.session.last_activity == 3
    assert not (paths.run / "demo.question.json").exists()


def test_commit_response_rejects_a_different_pending_question(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="x")
    SessionStore(paths).save(session)
    runtime = Runtime(session, paths, omp_path="fake")
    runtime.on_event({"type":"extension_ui_request","id":"q","title":"Pick"}, now=1)
    with pytest.raises(ValueError, match="pending question"):
        runtime.commit_response("other", "e", now=2)
    assert runtime.question is not None and runtime.question.id == "q"
    assert "e" not in runtime.seen


def test_run_transactional_response_failed_flush_preserves_question_and_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, _ = _transactional_runtime(tmp_path, monkeypatch, "failure")
    try:
        assert run("demo", paths=paths) == 0
        reloaded = Runtime(SessionStore(paths).load("demo"), paths, omp_path="fake")
        assert reloaded.question is not None and reloaded.question.id == "q"
        assert "e" not in reloaded.seen
        assert (paths.inbox / "demo" / "e.json").exists()
        assert not (paths.inbox / "demo" / "processed" / "e.json").exists()
    finally:
        _stop_fakes(children)


def test_run_transactional_response_flushes_complete_frame_before_commit_and_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, observed = _transactional_runtime(tmp_path, monkeypatch, "success")
    original_commit = Runtime.commit_response

    def commit_after_child_observation(runtime: Runtime, question_id: str, event_id: str = "", now: float | None = None) -> None:
        assert (paths.run / "demo.question.json").exists()
        assert (paths.inbox / "demo" / "e.json").exists()
        deadline = time.time() + 2
        while time.time() < deadline and not observed.exists():
            time.sleep(0.01)
        assert observed.exists()
        original_commit(runtime, question_id, event_id, now)

    monkeypatch.setattr(Runtime, "commit_response", commit_after_child_observation)
    try:
        assert run("demo", paths=paths) == 0
        assert observed.read_bytes() == b'{"type": "extension_ui_response", "id": "q", "value": "A"}\n'
        reloaded = Runtime(SessionStore(paths).load("demo"), paths, omp_path="fake")
        assert reloaded.question is None and "e" in reloaded.seen
        assert not (paths.inbox / "demo" / "e.json").exists()
        assert (paths.inbox / "demo" / "processed" / "e.json").exists()
    finally:
        _stop_fakes(children)


def test_runtime_rejects_expired_question(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",question_ttl=1)
    runtime.on_event({"type":"extension_ui_request","id":"q","title":"x"},now=1)
    assert runtime.accept_inbound({"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"yes"},now=3).terminal is True


def test_runtime_auto_answers_only_safe_question_after_commit(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x")
    SessionStore(paths).save(session); runtime=Runtime(session,paths,omp_path="fake",auto_answer_safe=True)
    response=runtime.on_event({"type":"extension_ui_request","id":"q","title":"Continue","options":[{"label":"Yes","recommended":True,"reversible":True}]},now=1)
    assert response == {"rpc":{"type":"extension_ui_response","id":"q","value":"Yes"},"question_id":"q"}
    assert runtime.question is not None and runtime.question.id == "q"
    runtime.commit_response(response["question_id"], now=2)
    assert runtime.question is None


def test_runtime_reloads_pending_question_and_replay_ids(tmp_path: Path) -> None:
    paths=Paths(tmp_path/"omp"); session=Session.new(name="demo",cwd=str(tmp_path),model="m",mission="x",platform="p",chat="c",topic="t")
    SessionStore(paths).save(session); first=Runtime(session,paths,omp_path="fake")
    first.on_event({"type":"extension_ui_request","id":"q","method":"select","title":"Pick","options":[{"label":"A"}]},now=1)
    accepted={"event_id":"e","question_id":"q","platform":"p","chat":"c","topic":"t","user":"","answer":"1"}
    result = first.accept_inbound(accepted,now=2)
    assert result.response["value"] == "A"
    first.commit_response(result.question_id, "e", now=2)
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

def test_rpc_line_buffer_handles_fragmented_adjacent_and_crlf_frames() -> None:
    buffer = RpcLineBuffer()
    assert buffer.feed(b'{"type":"message') == []
    assert buffer.feed(b'_end"}\r\n{"type":"turn_end"}\npart') == [
        '{"type":"message_end"}',
        '{"type":"turn_end"}',
    ]
    assert buffer.finish() == "part"


def test_rpc_line_buffer_reconstructs_fragmented_multibyte_utf8() -> None:
    data = '{"type":"message_end","content":"caf\u00e9"}\n'.encode()
    split = data.index("\u00e9".encode()) + 1
    buffer = RpcLineBuffer()
    assert buffer.feed(data[:split]) == []
    assert buffer.feed(data[split:]) == ['{"type":"message_end","content":"caf\u00e9"}']
    assert buffer.finish() == ""


def test_run_logs_valid_unterminated_rpc_as_redacted_residue_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, _ = _transactional_runtime(tmp_path, monkeypatch, "residue")
    try:
        assert run("demo", paths=paths) == 0
        events = [json.loads(line) for line in (paths.logs / "demo.jsonl").read_text().splitlines()]
        assert events == [{"type":"unparsed","content":'{"type": "extension_ui_request", "id": "q", "method": "select", "title": "Pick", "options": [{"label": "A"}], "token": "[REDACTED]"}'}]
        assert not (paths.run / "demo.question.json").exists()
        assert not (paths.outbox / "demo.json").exists()
    finally:
        _stop_fakes(children)


def test_run_bounds_post_exit_drain_when_descendant_inherits_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, _ = _transactional_runtime(tmp_path, monkeypatch, "inherited_stdout")
    descendant_done = tmp_path / "descendant.done"
    started = time.monotonic()
    try:
        assert run("demo", paths=paths) == 0
        assert time.monotonic() - started < 1.0
        events = [json.loads(line) for line in (paths.logs / "demo.jsonl").read_text().splitlines()]
        assert {"type":"turn_end","content":"descendant-output"} in events
        assert children[0].stdout is not None and children[0].stdout.closed
    finally:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not descendant_done.exists():
            time.sleep(0.01)
        assert descendant_done.exists()
        _stop_fakes(children)



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
