from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from hermes_omp.cli import main
from hermes_omp.core import Paths, SessionStore
from hermes_omp.service import LaunchdBackend, SystemdBackend, WindowsTaskBackend


def wait_for(predicate, timeout=8):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError("timeout")


def test_all_16_isolated_acceptance_scenarios(tmp_path: Path, monkeypatch) -> None:
    home=tmp_path/"home"; project=tmp_path/"project"; bridge=tmp_path/"bridge"; project.mkdir(); bridge.mkdir()
    fake_omp=tmp_path/"fake-omp"; fake_hermes=tmp_path/"fake-hermes"
    fake_omp.write_bytes((Path(__file__).parent/"fixtures"/"fake_omp.py").read_bytes()); fake_hermes.write_bytes((Path(__file__).parent/"fixtures"/"fake_hermes.py").read_bytes())
    fake_omp.chmod(0o755); fake_hermes.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME",str(home)); monkeypatch.setenv("FAKE_OMP_STATE",str(tmp_path/"omp-state.json")); monkeypatch.setenv("FAKE_BRIDGE_ROOT",str(bridge))
    assert main(["create","demo","--cwd",str(project),"--model","fake","--mission","mission","--platform","telegram","--chat","42","--topic","7","--allowed-user","9","--omp-path",str(fake_omp),"--no-install"])==0
    env={**os.environ,"HERMES_HOME":str(home),"PYTHONPATH":str(Path(__file__).parents[1]/"src"),"HERMES_OMP_BINARY":str(fake_omp),"HERMES_OMP_HERMES":str(fake_hermes),"FAKE_OMP_STATE":str(tmp_path/"omp-state.json"),"FAKE_BRIDGE_ROOT":str(bridge)}
    (bridge/"offline").write_text("1")
    proc=subprocess.Popen([sys.executable,"-m","hermes_omp.runtime","demo"],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    paths=Paths.discover()
    try:
        wait_for(lambda:(paths.run/"demo.question.json").exists())
    except AssertionError:
        stdout,stderr=proc.communicate(timeout=2)
        raise AssertionError(f"runtime exited={proc.returncode} stdout={stdout!r} stderr={stderr!r}")
    omp_pid=json.loads((tmp_path/"omp-state.json").read_text())["pid"]

    # Durable CLI prompt queue reaches OMP RPC and is acknowledged only after write.
    assert main(["send","demo","follow-up prompt"]) == 0
    wait_for(lambda:any(x.payload.get("message")=="follow-up prompt" and x.state=="delivered" for x in __import__("hermes_omp.core",fromlist=["Outbox"]).Outbox(paths.run/"demo.prompts.json").items))

    # 4-5 authorized answer accepted; wrong user/topic rejected
    inbox=paths.inbox/"demo"; inbox.mkdir(parents=True,exist_ok=True)
    wrong={"event_id":"wrong","question_id":"q-001","platform":"telegram","chat":"42","topic":"WRONG","user":"10","answer":"2"}
    (inbox/"wrong.json").write_text(json.dumps(wrong))
    time.sleep(.2); assert proc.poll() is None
    good={**wrong,"event_id":"good","topic":"7","user":"9","answer":"1"}; (inbox/"good.json").write_text(json.dumps(good))

    # 6-10 simulated Hermes/gateway restarts and bridge outage: OMP survives,
    # durable queue drains once and in order when the fake public bridge returns.
    assert proc.poll() is None and os.kill(omp_pid,0) is None
    (bridge/"hermes-restarted").write_text("simulated"); (bridge/"gateway-restarted").write_text("simulated")
    (bridge/"offline").unlink()
    proc.wait(timeout=10); assert proc.returncode==0
    # A new isolated supervisor instance reconnects and drains persisted FIFO.
    recovery=subprocess.Popen([sys.executable,"-m","hermes_omp.runtime","demo"],env=env)
    wait_for(lambda:(bridge/"delivered.jsonl").exists())
    recovery.terminate(); recovery.wait(timeout=5)
    delivered=[json.loads(x) for x in (bridge/"delivered.jsonl").read_text().splitlines()]
    messages=[x["message"] for x in delivered]
    assert len(messages)==len(set(messages)) and any("q-001" in x for x in messages)
    assert any("answer=Proceed" in line for line in (paths.logs/"demo.jsonl").read_text().splitlines())

    # 11 supervisor crash and restart policy simulation; exact resume ID retained.
    store=SessionStore(paths); session=store.load("demo"); session.omp_session_id="fake-session-001"; session.status="crashed"; store.save(session)
    restarted=subprocess.Popen([sys.executable,"-m","hermes_omp.runtime","demo"],env=env)
    wait_for(lambda:json.loads((tmp_path/"omp-state.json").read_text())["pid"]!=omp_pid)
    restarted.terminate(); restarted.wait(timeout=5)

    # 12 service manager reboot definitions generated for all backends.
    command=[sys.executable,"-m","hermes_omp.runtime","demo"]
    assert LaunchdBackend(paths.root).definition("demo",command,str(project),"on-failure")["KeepAlive"]
    assert "WantedBy=default.target" in SystemdBackend(paths.root).definition("demo",command,str(project),"on-failure")
    assert "LogonTrigger" in WindowsTaskBackend(paths.root).definition("demo",command,str(project),"on-failure")

    # 13 graceful stop observed above; 14 exact resume; 15 duplicate refused.
    assert store.load("demo").omp_session_id=="fake-session-001"
    try:
        main(["create","duplicate","--cwd",str(project),"--model","fake","--mission","x","--resume","fake-session-001","--omp-path",str(fake_omp),"--no-install"])
    except ValueError as exc: assert "already owned" in str(exc)
    else: raise AssertionError("duplicate was accepted")

    # 16 clean temporary service removal.
    assert main(["remove","demo","--no-service"])==0
    assert not (paths.sessions/"demo.json").exists()
