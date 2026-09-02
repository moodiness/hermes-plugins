from __future__ import annotations

import json
import os
import subprocess
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
import hermes_omp.runtime as runtime_module
import hermes_omp.cli as cli_module

from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.bridge import FileInbox, HermesSendBridge
from hermes_omp.runtime import RpcLineBuffer, Runtime, _terminate_child, acquire_owner_lock, build_omp_command, inspect_adoption, release_owner_lock, run

WINDOWS_PIPE_SELECTOR_REASON = "subprocess-pipe selector integration is not validated on native Windows"

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
                "import json, os, signal, sys, time\n"
                "from pathlib import Path\n"
                "done = Path(os.environ['FAKE_DESC_DONE'])\n"
                "def terminate(*_):\n"
                "    done.write_text('term')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, terminate)\n"
                "print(json.dumps({'type':'turn_end','content':'descendant-output'}), flush=True)\n"
                "Path(os.environ['FAKE_DESC_READY']).write_text('ready')\n"
                "time.sleep(3)\n"
                "done.write_text('timeout')\n"
            ) + """
            subprocess.Popen([sys.executable, "-c", code], stdout=sys.stdout, stderr=sys.stderr)
            while not Path(os.environ["FAKE_DESC_READY"]).exists():
                time.sleep(0.01)
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


def _write_orphan_fake(tmp_path: Path) -> Path:
    fake = tmp_path / "orphan_fake_omp.py"
    fake.write_text(
        """import os
import sys
from pathlib import Path

Path(os.environ["FAKE_OMP_PID"]).write_text(str(os.getpid()))
Path(os.environ["FAKE_MALFORMED_INBOX"]).write_text("{")
print("{\\"type\\":\\"ready\\"}", flush=True)
for _ in sys.stdin:
    pass
""",
        encoding="utf-8",
    )
    return fake


def _write_term_ignoring_fake(tmp_path: Path) -> Path:
    fake = tmp_path / "term_ignoring_fake_omp.py"
    fake.write_text(
        """import os
import signal
import sys
import threading
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["FAKE_OMP_PID"]).write_text(str(os.getpid()))
print('{"type":"ready"}', flush=True)

def watchdog():
    time.sleep(7)
    os._exit(7)

threading.Thread(target=watchdog, daemon=True).start()
for _ in sys.stdin:
    pass
""",
        encoding="utf-8",
    )
    return fake


def _write_finite_cleanup_fake(tmp_path: Path) -> Path:
    fake = tmp_path / "finite_cleanup_fake.py"
    fake.write_text(
        """import os
import time
from pathlib import Path

Path(os.environ["FAKE_CHILD_STARTED"]).write_text("started")
Path(os.environ["FAKE_MALFORMED_INBOX"]).write_text("{")
print('{"type":"ready"}', flush=True)
time.sleep(10)
Path(os.environ["FAKE_CHILD_DONE"]).write_text("done")
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
    monkeypatch.setenv("FAKE_DESC_READY", str(tmp_path / "descendant.ready"))
    monkeypatch.setenv("FAKE_DESC_DONE", str(tmp_path / "descendant.done"))
    inbox = paths.inbox / "demo"
    inbox.mkdir(parents=True, exist_ok=True)
    event = {"event_id":"e","question_id":"q","platform":"telegram","chat":"42","topic":"7","user":"9","answer":"1"}
    (inbox / "e.json").write_text(json.dumps(event), encoding="utf-8")
    return paths, children, observed


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.returncode is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _pid_alive(pid: int) -> bool:
    return runtime_module._pid_alive(pid)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _stop_fakes(children: list[subprocess.Popen[str]]) -> None:
    for child in children:
        if child.returncode is None:
            _terminate_child(child, timeout=0.5)




@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ordering contract")
def test_terminate_child_signals_group_before_reaping(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    killed = False

    class Child:
        pid = 12345
        returncode = None

        def poll(self):
            events.append("poll")
            if killed:
                self.returncode = -signal.SIGKILL
            return self.returncode

        def wait(self, timeout):
            events.append("wait")
            self.returncode = -signal.SIGKILL
            return self.returncode

    child = Child()

    def killpg(_pgid, sig):
        nonlocal killed
        if sig == 0:
            events.append("probe")
            if child.returncode is not None:
                raise ProcessLookupError
        elif sig == signal.SIGTERM:
            events.append("term")
        elif sig == signal.SIGKILL:
            events.append("kill")
            killed = True

    monkeypatch.setattr("hermes_omp.runtime.os.killpg", killpg)
    _terminate_child(child, timeout=0)
    completed_events = list(events)
    _terminate_child(child, timeout=0)

    assert events == completed_events
    assert events.index("term") < events.index("kill") < events.index("wait")
@pytest.mark.parametrize(
    ("wait_result", "last_error", "expected"),
    [(0x102, 0, True), (0, 0, False), (None, 87, False), (None, 5, True), (0xFFFFFFFF, 0, True)],
)
def test_windows_pid_probe_is_non_destructive_and_conservative(wait_result, last_error, expected) -> None:
    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    class Kernel:
        def __init__(self):
            self.closed = []
            self.OpenProcess = Function(lambda *_: 0 if wait_result is None else 123)
            self.WaitForSingleObject = Function(lambda *_: wait_result)
            self.CloseHandle = Function(lambda handle: self.closed.append(handle) or True)

    kernel = Kernel()
    alive = runtime_module._windows_pid_alive(42, kernel32=kernel, get_last_error=lambda: last_error)
    assert alive is expected
    assert kernel.closed == ([] if wait_result is None else [123])


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle liveness contract")
def test_windows_pid_liveness_probe_does_not_terminate_real_child(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], text=True)
    lock = tmp_path / "owner"
    lock.write_text(json.dumps({"pid":child.pid,"session_id":"session","token":"test"}))
    try:
        assert runtime_module._pid_alive(child.pid)
        assert runtime_module.owner_lock_live(lock)
        assert child.poll() is None
    finally:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)



@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group grace contract")
def test_terminate_child_gives_live_group_full_term_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    killed_at = 0.0

    class Child:
        pid = 12345
        returncode = None

        def poll(self):
            if killed_at:
                self.returncode = -signal.SIGKILL
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    child = Child()

    def killpg(_pgid, sig):
        nonlocal killed_at
        if sig == signal.SIGKILL:
            killed_at = time.monotonic()
        elif sig == 0 and child.returncode is not None:
            raise ProcessLookupError

    monkeypatch.setattr("hermes_omp.runtime.os.killpg", killpg)
    started = time.monotonic()
    _terminate_child(child, timeout=0.05)
    assert killed_at - started >= 0.04



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


def test_runtime_touch_merges_activity_without_overwriting_config(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    store = SessionStore(paths)
    session = Session.new(name="demo", cwd=str(tmp_path), model="old", mission="x")
    store.save(session)
    runtime = Runtime(session, paths, omp_path="fake")
    updated = store.load("demo")
    updated.model = "new"
    store.save(updated)

    runtime._touch(42.0)

    persisted = store.load("demo")
    assert persisted.model == "new"
    assert persisted.last_activity == 42.0


def test_runtime_startup_and_remove_share_identity_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "omp")
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="x")
    SessionStore(paths).save(session)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    startup_entered = threading.Event()
    release_startup = threading.Event()
    remove_finished = threading.Event()
    outcomes: list[object] = []

    def blocked_owner_lock(lock: Path, session_id: str):
        startup_entered.set()
        assert release_startup.wait(2)
        raise RuntimeError("stop before spawn")

    monkeypatch.setattr(runtime_module, "acquire_owner_lock", blocked_owner_lock)

    def start_runtime() -> None:
        try:
            runtime_module.run("demo", paths=paths)
        except BaseException as exc:
            outcomes.append(exc)

    def remove_session() -> None:
        outcomes.append(cli_module.main(["remove", "demo", "--no-service"]))
        remove_finished.set()

    starter = threading.Thread(target=start_runtime)
    remover = threading.Thread(target=remove_session)
    starter.start()
    assert startup_entered.wait(2)
    remover.start()
    remove_overlapped = remove_finished.wait(0.25)
    release_startup.set()
    starter.join(2)
    remover.join(2)

    assert not starter.is_alive() and not remover.is_alive()
    assert not remove_overlapped
    assert any(isinstance(result, RuntimeError) for result in outcomes)
    assert 0 in outcomes
    assert not (paths.sessions / "demo.json").exists()



def test_delayed_service_runtime_rejects_replaced_session_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "omp")
    store = SessionStore(paths)
    original = Session.new(name="demo", cwd=str(tmp_path), model="old", mission="x")
    replacement = Session.new(name="demo", cwd=str(tmp_path), model="replacement", mission="y")
    store.save(original)
    store.replace(replacement)
    spawned: list[object] = []
    monkeypatch.setattr(runtime_module.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args) or None)

    with pytest.raises(RuntimeError, match="session identity changed"):
        runtime_module.run("demo", paths=paths, expected_session_id=original.id)

    assert spawned == []
    assert store.load("demo") == replacement
    assert not (paths.run / "demo.owner").exists()

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


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
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


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
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


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
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


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_terminates_inherited_stdout_group_then_drains_complete_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, _ = _transactional_runtime(tmp_path, monkeypatch, "inherited_stdout")
    descendant_done = tmp_path / "descendant.done"
    try:
        assert run("demo", paths=paths) == 0
        descendant_stopped = descendant_done.read_text() == "term"
        events = [json.loads(line) for line in (paths.logs / "demo.jsonl").read_text().splitlines()]
        frame_was_drained = {"type":"turn_end","content":"descendant-output"} in events
        reloaded = SessionStore(paths).load("demo")
        final_state = (reloaded.status, reloaded.supervisor_pid, reloaded.omp_pid)
        stdout_was_closed = children[0].stdout is not None and children[0].stdout.closed
    finally:
        _stop_fakes(children)
        _wait_until(descendant_done.exists, timeout=4)
    assert descendant_stopped
    assert frame_was_drained
    assert stdout_was_closed
    assert final_state == ("completed", 0, 0)


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_stop_escalates_term_ignoring_child_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "omp")
    fake = _write_term_ignoring_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    pid_path = tmp_path / "fake.pid"
    children: list[subprocess.Popen[str]] = []
    handlers: dict[int, object] = {}
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_PID", str(pid_path))

    def request_stop() -> None:
        assert _wait_until(pid_path.exists)
        handlers[signal.SIGTERM]()

    stopper = threading.Thread(target=request_stop)
    stopper.start()
    started = time.monotonic()
    try:
        code = run("demo", paths=paths)
        elapsed = time.monotonic() - started
        state = SessionStore(paths).load("demo")
    finally:
        stopper.join(timeout=3)
        _stop_fakes(children)
    assert code != 7
    assert (state.status, state.supervisor_pid, state.omp_pid) == ("stopped", 0, 0)
    assert elapsed < 6.5

@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_persists_child_marker_before_processing_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "omp")
    fake = _write_orphan_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def inspect_then_fail(_inbox):
        owner = json.loads((paths.run / "demo.owner").read_text())
        assert owner["orphaned_pgid"] == children[0].pid
        raise json.JSONDecodeError("malformed", "{", 0)

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda *_: None)
    monkeypatch.setattr(FileInbox, "poll", inspect_then_fail)
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_PID", str(tmp_path / "fake.pid"))
    monkeypatch.setenv("FAKE_MALFORMED_INBOX", str(tmp_path / "malformed.json"))
    try:
        with pytest.raises(json.JSONDecodeError):
            run("demo", paths=paths)
    finally:
        _stop_fakes(children)


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_cleans_child_when_proactive_marker_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "omp")
    fake = _write_orphan_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda *_: None)
    monkeypatch.setattr("hermes_omp.runtime.atomic_write", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("marker write failed")))
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_PID", str(tmp_path / "fake.pid"))
    monkeypatch.setenv("FAKE_MALFORMED_INBOX", str(tmp_path / "malformed.json"))
    try:
        with pytest.raises(OSError, match="marker write failed"):
            run("demo", paths=paths)
        child_reaped = children[0].returncode is not None
        state = SessionStore(paths).load("demo")
        lock_absent = not (paths.run / "demo.owner").exists()
    finally:
        _stop_fakes(children)
    assert child_reaped
    assert (state.status, state.supervisor_pid, state.omp_pid) == ("crashed", 0, 0)
    assert lock_absent


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_preserves_legacy_exception_when_cleanup_initially_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class LegacyError(Exception):
        add_note = None

    paths = Paths(tmp_path / "omp")
    fake = _write_orphan_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen
    production_cleanup = _terminate_child
    attempts = 0
    original = LegacyError("legacy body failure")

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def flaky_cleanup(child, timeout=5.0):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("initial cleanup failure")
        return production_cleanup(child, timeout=0.5)

    def fail_poll(_inbox):
        raise original

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda *_: None)
    monkeypatch.setattr("hermes_omp.runtime._terminate_child", flaky_cleanup)
    monkeypatch.setattr(FileInbox, "poll", fail_poll)
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_PID", str(tmp_path / "fake.pid"))
    monkeypatch.setenv("FAKE_MALFORMED_INBOX", str(tmp_path / "malformed.json"))
    try:
        with pytest.raises(LegacyError) as caught:
            run("demo", paths=paths)
        retained_state = SessionStore(paths).load("demo")
        lock_payload = json.loads((paths.run / "demo.owner").read_text())
    finally:
        _stop_fakes(children)
    assert caught.value is original
    assert attempts == 1
    assert (retained_state.supervisor_pid, retained_state.omp_pid) == (os.getpid(), children[0].pid)
    assert lock_payload["orphaned_pgid"] == children[0].pid


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_does_not_mistake_callers_active_exception_for_body_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, children, _ = _transactional_runtime(tmp_path, monkeypatch, "success")
    production_cleanup = _terminate_child
    calls = 0

    def fail_finally_once(child, timeout=5.0):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("final cleanup failure")
        return production_cleanup(child, timeout=0.5)

    monkeypatch.setattr("hermes_omp.runtime._terminate_child", fail_finally_once)
    caller_error = ValueError("caller's active exception")
    try:
        try:
            raise caller_error
        except ValueError:
            with pytest.raises(RuntimeError, match="final cleanup failure"):
                run("demo", paths=paths)
    finally:
        _stop_fakes(children)
    assert not getattr(caller_error, "__notes__", [])


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_failed_cleanup_leaves_recoverable_orphan_marker_after_supervisor_exit(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "omp")
    fake = _write_finite_cleanup_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    inbox = paths.inbox / "demo"; inbox.mkdir(parents=True, exist_ok=True)
    unexpected_retry = tmp_path / "unexpected-retry"
    body_error = tmp_path / "body-error"
    harness = """import json, os, sys
from pathlib import Path
from hermes_omp import runtime
from hermes_omp.core import Paths

real_cleanup = runtime._terminate_child
attempts = 0
def fail_once(child, timeout=5.0):
    global attempts
    attempts += 1
    if attempts == 1:
        raise RuntimeError('controlled cleanup failure')
    Path(os.environ['UNEXPECTED_RETRY']).write_text('retried')
    return real_cleanup(child, timeout=0.5)

runtime._terminate_child = fail_once
try:
    runtime.run('demo', paths=Paths(Path(os.environ['OMP_ROOT'])))
except json.JSONDecodeError:
    Path(os.environ['BODY_ERROR']).write_text('JSONDecodeError')
    raise SystemExit(23)
"""
    child_done = tmp_path / "child-done"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "HERMES_OMP_BINARY": sys.executable,
        "OMP_ROOT": str(paths.root),
        "FAKE_CHILD_STARTED": str(tmp_path / "child-started"),
        "FAKE_CHILD_DONE": str(child_done),
        "FAKE_MALFORMED_INBOX": str(inbox / "malformed.json"),
        "UNEXPECTED_RETRY": str(unexpected_retry),
        "BODY_ERROR": str(body_error),
    }
    supervisor = subprocess.Popen([sys.executable, "-c", harness], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    try:
        supervisor.wait(timeout=6)
        stdout = stderr = ""
        lock_payload = json.loads((paths.run / "demo.owner").read_text())
        retained_state = SessionStore(paths).load("demo")
        with pytest.raises(RuntimeError, match="orphaned child"):
            acquire_owner_lock(paths.run / "demo.owner", session.id)
        lock_refused_while_orphan_lived = (paths.run / "demo.owner").exists()
    finally:
        _stop_process(supervisor)
        if (paths.run / "demo.owner").exists():
            _wait_until(child_done.exists, timeout=11)

    recovered = False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not recovered:
        try:
            fd, token = acquire_owner_lock(paths.run / "demo.owner", session.id)
        except RuntimeError:
            time.sleep(0.01)
        else:
            recovered = True
            release_owner_lock(paths.run / "demo.owner", fd, token)
    assert supervisor.returncode == 23, (stdout, stderr)
    assert body_error.read_text() == "JSONDecodeError"
    assert not unexpected_retry.exists()
    assert lock_payload["pid"] == supervisor.pid
    assert lock_payload["orphaned_pgid"] == retained_state.omp_pid
    assert retained_state.supervisor_pid == supervisor.pid and retained_state.omp_pid > 0
    assert lock_refused_while_orphan_lived
    assert recovered


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_run_malformed_inbox_reaps_owned_orphan_before_clearing_state_and_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "omp")
    fake = _write_orphan_fake(tmp_path)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="mission", omp_options=[str(fake)])
    SessionStore(paths).save(session)
    pid_path = tmp_path / "fake.pid"
    inbox = paths.inbox / "demo"
    inbox.mkdir(parents=True, exist_ok=True)
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("hermes_omp.runtime.subprocess.Popen", capture_child)
    monkeypatch.setattr("hermes_omp.runtime.signal.signal", lambda *_: None)
    monkeypatch.setattr(HermesSendBridge, "deliver", lambda *_: None)
    monkeypatch.setenv("FAKE_MALFORMED_INBOX", str(inbox / "malformed.json"))
    monkeypatch.setenv("HERMES_OMP_BINARY", sys.executable)
    monkeypatch.setenv("FAKE_OMP_PID", str(pid_path))
    child_dead = False
    try:
        with pytest.raises(json.JSONDecodeError):
            run("demo", paths=paths)
        assert _wait_until(pid_path.exists)
        child_pid = int(pid_path.read_text())
        child_dead = _wait_until(lambda: not _pid_alive(child_pid))
        reloaded = SessionStore(paths).load("demo")
        final_state = (reloaded.status, reloaded.supervisor_pid, reloaded.omp_pid)
        lock_absent = not (paths.run / "demo.owner").exists()
    finally:
        _stop_fakes(children)
    assert child_dead
    assert final_state == ("crashed", 0, 0)
    assert lock_absent



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
