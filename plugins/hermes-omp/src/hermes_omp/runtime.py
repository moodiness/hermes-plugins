from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .bridge import FileInbox, HermesSendBridge
from .core import Authorization, Outbox, Paths, Question, Session, SessionStore, atomic_write, classify_safe_answer, parse_rpc_line, redact


def build_omp_command(session: Session, omp_path: str) -> list[str]:
    command = [omp_path, *session.omp_options, "--mode", "rpc", "--model", session.model]
    if session.omp_session_id: command += ["--resume", session.omp_session_id]
    return command


def inspect_adoption(argv: list[str], cwd: str) -> dict[str, str]:
    try: sid = argv[argv.index("--resume") + 1]
    except (ValueError, IndexError):
        matches = [x.split("=",1)[1] for x in argv if x.startswith("--resume=")]
        sid = matches[0] if matches else ""
    if not sid: raise ValueError("cannot adopt without explicit --resume OMP session ID")
    try: model = argv[argv.index("--model") + 1]
    except (ValueError, IndexError): model = next((x.split("=",1)[1] for x in argv if x.startswith("--model=")), "")
    return {"omp_session_id": sid, "model": model, "cwd": cwd}


class Runtime:
    def __init__(self, session: Session, paths: Paths, *, omp_path: str, question_ttl: float = 86400, auto_answer_safe: bool = False):
        self.session, self.paths, self.omp_path = session, paths, omp_path
        self.store = SessionStore(paths); self.question_ttl=question_ttl; self.auto_answer_safe=auto_answer_safe
        self.question: Optional[Question] = None; self.seen: set[str] = set()
        self.auth=Authorization(session.platform,session.chat,session.topic,tuple(session.allowed_users))

    def startup_frames(self) -> list[dict[str, Any]]:
        frames=[{"type":"negotiate_protocol","protocolVersion":2,"id":"negotiate"}]
        if self.session.mission: frames.append({"type":"prompt","message":self.session.mission,"id":"initial"})
        return frames

    def _touch(self, now: Optional[float]=None) -> None:
        self.session.last_activity=time.time() if now is None else now; self.store.save(self.session)

    def on_event(self,event:dict[str,Any],now:Optional[float]=None) -> Optional[dict[str,Any]]:
        self._touch(now)
        if event.get("type") != "extension_ui_request" or event.get("method","select") not in {"select","confirm","input","ask"}: return None
        self.question=Question.from_event(event,self.session.name,self.question_ttl,now)
        atomic_write(self.paths.run/f"{self.session.name}.question.json",json.dumps({"id":self.question.id,"expires_at":self.question.expires_at})+"\n")
        automatic=classify_safe_answer(self.question) if self.auto_answer_safe else None
        if automatic:
            rpc={"type":"extension_ui_response","id":self.question.id,"value":automatic}; self.question=None
            return {"rpc":rpc}
        return {"event_id":f"question-{self.session.id}-{self.question.id}","platform":self.session.platform,"chat":self.session.chat,"topic":self.session.topic,"text":self.question.message()}

    def accept_inbound(self,event:dict[str,Any],now:Optional[float]=None) -> Optional[dict[str,Any]]:
        stamp=time.time() if now is None else now
        if not self.question or stamp > self.question.expires_at or not self.auth.authorize(event,expected_question_id=self.question.id,seen_event_ids=self.seen): return None
        self.seen.add(str(event["event_id"])); value=str(event.get("answer","")).strip()
        if value.isdigit() and self.question.options:
            index=int(value)-1
            if not 0 <= index < len(self.question.options): return None
            value=self.question.options[index].label
        response={"type":"extension_ui_response","id":self.question.id,"value":value}
        self.question=None; self._touch(stamp); return response


def _event_text(event: dict[str,Any]) -> str:
    message=event.get("message")
    if isinstance(message,dict) and message.get("role")=="assistant":
        content=message.get("content","")
        if isinstance(content,str): return content
    return ""


def run(name: str, *, paths: Optional[Paths]=None) -> int:
    paths=paths or Paths.discover(); store=SessionStore(paths); session=store.load(name)
    lock=paths.run/f"{session.name}.owner"
    try: fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError: raise RuntimeError(f"session already owned: {session.name}")
    child=None; stopping=False
    def stop(*_):
        nonlocal stopping; stopping=True
        if child and child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    outbox=Outbox(paths.outbox/f"{session.name}.json"); inbox=FileInbox(paths.inbox/session.name); bridge=HermesSendBridge(hermes=os.environ.get("HERMES_OMP_HERMES","hermes"))
    runtime_path = paths.run / f"{name}.omp-path"
    configured_path = runtime_path.read_text().strip() if runtime_path.exists() else ""
    runtime=Runtime(session,paths,omp_path=os.environ.get("HERMES_OMP_BINARY", configured_path or "omp"),auto_answer_safe=os.environ.get("HERMES_OMP_AUTO_ANSWER_SAFE")=="1")
    log_path=paths.logs/f"{session.name}.jsonl"; log_path.parent.mkdir(parents=True,exist_ok=True)
    try:
        outbox=Outbox(paths.outbox/f"{session.name}.json")
        for item in outbox.due():
            try: bridge.deliver(item.payload); outbox.ack(item.id)
            except Exception as exc: outbox.fail(item.id,error=str(exc))
        child=subprocess.Popen(build_omp_command(session,runtime.omp_path),cwd=session.cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
        session.status="running"; session.supervisor_pid=os.getpid(); session.omp_pid=child.pid; store.save(session)
        assert child.stdin and child.stdout
        for frame in runtime.startup_frames(): child.stdin.write(json.dumps(frame)+"\n")
        child.stdin.flush(); selector=selectors.DefaultSelector(); selector.register(child.stdout,selectors.EVENT_READ)
        while child.poll() is None:
            for key,_ in selector.select(timeout=.1):
                while True:
                    line=os.read(key.fileobj.fileno(),65536).decode("utf-8",errors="replace")
                    if not line: break
                    buffered = line.splitlines(True)
                    for line in buffered:
                        try: event=parse_rpc_line(line)
                        except ValueError: event={"type":"unparsed","content":str(redact(line.strip()))}
                        with log_path.open("a",encoding="utf-8") as log: log.write(json.dumps(redact(event),ensure_ascii=False)+"\n")
                        action=runtime.on_event(event)
                        if action and action.get("rpc"): child.stdin.write(json.dumps(action["rpc"])+"\n"); child.stdin.flush()
                        elif action: outbox.enqueue(action["event_id"],action)
                        text=_event_text(event)
                        if text:
                            eid="progress-"+hashlib.sha256(text.encode()).hexdigest()[:24]; outbox.enqueue(eid,{"platform":session.platform,"chat":session.chat,"topic":session.topic,"text":text[:4000]})
                    break
            for event in inbox.poll():
                response=runtime.accept_inbound(event)
                inbox.ack(str(event.get("event_id")))
                if response: child.stdin.write(json.dumps(response)+"\n"); child.stdin.flush()
            for item in outbox.due():
                try: bridge.deliver(item.payload); outbox.ack(item.id)
                except Exception as exc: outbox.fail(item.id,error=str(exc))
        for item in outbox.due():
            try: bridge.deliver(item.payload); outbox.ack(item.id)
            except Exception as exc: outbox.fail(item.id,error=str(exc))
        code=int(child.returncode or 0); session.status="stopped" if stopping else ("completed" if code==0 else "crashed"); session.last_activity=time.time(); store.save(session); return code
    finally:
        os.close(fd)
        try: lock.unlink()
        except FileNotFoundError: pass


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser(); p.add_argument("name"); args=p.parse_args(argv); return run(args.name)

if __name__=="__main__": raise SystemExit(main())
