from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_omp.cli import main
from hermes_omp.core import Paths, SessionStore
from hermes_omp.service import LaunchdBackend, SystemdBackend, WindowsTaskBackend

WINDOWS_PIPE_SELECTOR_REASON = "subprocess-pipe selector integration is not validated on native Windows"


def wait_for(predicate, timeout=8):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError("timeout")


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason=WINDOWS_PIPE_SELECTOR_REASON)
def test_fake_process_queue_resume_and_service_definition_integration(tmp_path: Path, monkeypatch) -> None:
    home=tmp_path/"home"; project=tmp_path/"project"; bridge=tmp_path/"bridge"; project.mkdir(); bridge.mkdir()
    fake_omp=tmp_path/"fake-omp"; fake_hermes=tmp_path/"fake-hermes"
    fake_omp.write_bytes((Path(__file__).parent/"fixtures"/"fake_omp.py").read_bytes()); fake_hermes.write_bytes((Path(__file__).parent/"fixtures"/"fake_hermes.py").read_bytes())
    fake_omp.chmod(0o755); fake_hermes.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME",str(home)); monkeypatch.setenv("FAKE_OMP_STATE",str(tmp_path/"omp-state.json")); monkeypatch.setenv("FAKE_BRIDGE_ROOT",str(bridge)); monkeypatch.setenv("FAKE_OMP_EXIT_DELAY","0.05")
    assert main(["create","demo","--cwd",str(project),"--model","fake","--mission","mission","--platform","telegram","--chat","42","--topic","7","--allowed-user","9","--omp-path",str(fake_omp),"--no-install"])==0
    env={**os.environ,"HERMES_HOME":str(home),"PYTHONPATH":str(Path(__file__).parents[1]/"src"),"HERMES_OMP_BINARY":str(fake_omp),"HERMES_OMP_HERMES":str(fake_hermes),"FAKE_OMP_STATE":str(tmp_path/"omp-state.json"),"FAKE_BRIDGE_ROOT":str(bridge),"FAKE_OMP_EXIT_DELAY":"0.05"}
    children: list[subprocess.Popen[str]]=[]

    def spawn(*args, **kwargs) -> subprocess.Popen[str]:
        child=subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child

    paths=Paths.discover()
    try:
        (bridge/"offline").write_text("1")
        proc=spawn([sys.executable,"-m","hermes_omp.runtime","demo"],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            wait_for(lambda:(paths.run/"demo.question.json").exists())
        except AssertionError:
            stdout,stderr=proc.communicate(timeout=2)
            raise AssertionError(f"runtime exited={proc.returncode} stdout={stdout!r} stderr={stderr!r}")
        omp_pid=json.loads((tmp_path/"omp-state.json").read_text())["pid"]

        assert main(["send","demo","follow-up prompt"]) == 0
        wait_for(lambda:any(x.payload.get("message")=="follow-up prompt" and x.state=="delivered" for x in __import__("hermes_omp.core",fromlist=["Outbox"]).Outbox(paths.run/"demo.prompts.json").items))

        inbox=paths.inbox/"demo"; inbox.mkdir(parents=True,exist_ok=True)
        wrong={"event_id":"wrong","question_id":"q-001","platform":"telegram","chat":"42","topic":"WRONG","user":"10","answer":"2"}
        (inbox/"wrong.json").write_text(json.dumps(wrong))
        time.sleep(.2); assert proc.poll() is None
        good={**wrong,"event_id":"good","topic":"7","user":"9","answer":"1"}; (inbox/"good.json").write_text(json.dumps(good))

        assert proc.poll() is None and os.kill(omp_pid,0) is None
        (bridge/"offline").unlink()
        proc.wait(timeout=10); assert proc.returncode==0

        recovery=spawn([sys.executable,"-m","hermes_omp.runtime","demo"],env=env)
        wait_for(lambda:(bridge/"delivered.jsonl").exists())
        stop_process(recovery)
        delivered=[json.loads(x) for x in (bridge/"delivered.jsonl").read_text().splitlines()]
        messages=[x["message"] for x in delivered]
        assert len(messages)==len(set(messages)) and any("q-001" in x for x in messages)
        assert any("answer=Proceed" in line for line in (paths.logs/"demo.jsonl").read_text().splitlines())

        store=SessionStore(paths); session=store.load("demo"); session.omp_session_id="fake-session-001"; session.status="crashed"; store.save(session)
        restarted=spawn([sys.executable,"-m","hermes_omp.runtime","demo"],env=env)
        def restarted_with_resume() -> bool:
            try:
                state=json.loads((tmp_path/"omp-state.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return False
            return state["pid"]!=omp_pid and state["session_id"]=="fake-session-001"
        wait_for(restarted_with_resume)
        stop_process(restarted)

        command=[sys.executable,"-m","hermes_omp.runtime","demo"]
        assert LaunchdBackend(paths.root).definition("demo",command,str(project),"on-failure")["KeepAlive"]
        assert "WantedBy=default.target" in SystemdBackend(paths.root).definition("demo",command,str(project),"on-failure")
        assert "LogonTrigger" in WindowsTaskBackend(paths.root).definition("demo",command,str(project),"on-failure")

        assert store.load("demo").omp_session_id=="fake-session-001"
        assert main(["create","duplicate","--cwd",str(project),"--model","fake","--mission","x","--resume","fake-session-001","--omp-path",str(fake_omp),"--no-install","--json"]) != 0
        assert main(["remove","demo","--no-service"])==0
        assert not (paths.sessions/"demo.json").exists()
    finally:
        for child in reversed(children):
            stop_process(child)
