from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import selectors
import signal
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

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
        state_path=paths.run/f"{session.name}.runtime.json"
        self.state_path=state_path; state=json.loads(state_path.read_text()) if state_path.exists() else {}
        question=state.get("question")
        self.question: Optional[Question] = Question.from_dict(question) if question else None
        self.seen: set[str] = set(str(x) for x in state.get("seen_event_ids",[]))
        self.auth=Authorization(session.platform,session.chat,session.topic,tuple(session.allowed_users))

    def _save_state(self) -> None:
        atomic_write(self.state_path,json.dumps({"question":self.question.to_dict() if self.question else None,"seen_event_ids":sorted(self.seen)},indent=2)+"\n")

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
        self._save_state(); atomic_write(self.paths.run/f"{self.session.name}.question.json",json.dumps(self.question.to_dict())+"\n")
        automatic=classify_safe_answer(self.question) if self.auto_answer_safe else None
        if automatic:
            rpc={"type":"extension_ui_response","id":self.question.id,"value":automatic}
            return {"rpc":rpc,"question_id":self.question.id}
        return {"event_id":f"question-{self.session.id}-{self.question.id}","platform":self.session.platform,"chat":self.session.chat,"topic":self.session.topic,"text":self.question.message()}

    def accept_inbound(self,event:dict[str,Any],now:Optional[float]=None) -> "InboundResult":
        stamp=time.time() if now is None else now
        event_id=str(event.get("event_id", ""))
        if not self.question: return InboundResult(terminal=event_id in self.seen,retryable=event_id not in self.seen)
        if stamp > self.question.expires_at or not self.auth.authorize(event,expected_question_id=self.question.id,seen_event_ids=self.seen):
            if event_id: self.seen.add(event_id); self._save_state()
            return InboundResult(terminal=True)
        value=str(event.get("answer","")).strip()
        if value.isdigit() and self.question.options:
            index=int(value)-1
            if not 0 <= index < len(self.question.options):
                self.seen.add(event_id); self._save_state(); return InboundResult(terminal=True)
            value=self.question.options[index].label
        response={"type":"extension_ui_response","id":self.question.id,"value":value}
        return InboundResult(response=response, question_id=self.question.id)

    def commit_response(self, question_id: str, event_id: str = "", now: Optional[float] = None) -> None:
        if not self.question or self.question.id != question_id:
            raise ValueError("response does not match the pending question")
        if event_id:
            self.seen.add(event_id)
        self.question=None
        self._save_state()
        (self.paths.run/f"{self.session.name}.question.json").unlink(missing_ok=True)
        self._touch(now)


class InboundResult(NamedTuple):
    response: Optional[dict[str,Any]] = None
    retryable: bool = False
    terminal: bool = False
    question_id: str = ""

class RpcLineBuffer:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def feed(self, data: bytes) -> list[str]:
        self._buffer += self._decoder.decode(data)
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()
        return [line[:-1] if line.endswith("\r") else line for line in parts]

    def finish(self) -> str:
        self._buffer += self._decoder.decode(b"", final=True)
        residue = self._buffer
        self._buffer = ""
        return residue


def _pid_alive(pid: int) -> bool:
    if pid <= 0: return False
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except PermissionError: return True
    return True


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_child(child: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if getattr(child, "_hermes_omp_cleanup_done", False):
        return

    timeout = max(0.0, timeout)
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
        if child.poll() is None:
            child.wait(timeout=max(0.1, timeout))
    else:
        pgid = child.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        while time.monotonic() < deadline:
            child.poll()
            if not _process_group_alive(pgid):
                break
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if _process_group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            try:
                child.wait(timeout=max(0.1, min(timeout, 0.5)))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=0.5)

    setattr(child, "_hermes_omp_cleanup_done", True)


def acquire_owner_lock(lock: Path, session_id: str) -> tuple[int,str]:
    token=secrets.token_hex(16); payload=json.dumps({"pid":os.getpid(),"session_id":session_id,"token":token})+"\n"
    while True:
        try: fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            try: current=json.loads(lock.read_text())
            except (OSError,ValueError): raise RuntimeError(f"session owner lock is unreadable: {lock}")
            if _pid_alive(int(current.get("pid",0))): raise RuntimeError("session already owned")
            if str(current.get("session_id")) != session_id: raise RuntimeError("owner lock belongs to a different session")
            stale=lock.with_name(f"{lock.name}.stale-{token}")
            try: os.replace(lock,stale)
            except FileNotFoundError: continue
            stale.unlink(missing_ok=True); continue
        os.write(fd,payload.encode()); os.fsync(fd); return fd,token


def release_owner_lock(lock: Path, fd: int, token: str) -> None:
    os.close(fd)
    try: current=json.loads(lock.read_text())
    except (OSError,ValueError): return
    if current.get("pid")==os.getpid() and current.get("token")==token: lock.unlink(missing_ok=True)


def _event_text(event: dict[str,Any]) -> str:
    message=event.get("message")
    if isinstance(message,dict) and message.get("role")=="assistant":
        content=message.get("content","")
        if isinstance(content,str): return content
    return ""


def run(name: str, *, paths: Optional[Paths]=None) -> int:
    paths=paths or Paths.discover(); store=SessionStore(paths); session=store.load(name)
    lock=paths.run/f"{session.name}.owner"
    fd,lock_token=acquire_owner_lock(lock,session.id)
    child: Optional[subprocess.Popen[str]]=None
    selector: Optional[selectors.BaseSelector]=None
    line_buffer: Optional[RpcLineBuffer]=None
    terminal_state_saved=False
    stopping=False

    def stop(*_):
        nonlocal stopping
        stopping=True
        if child and child.poll() is None:
            child.terminate()

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
        line_buffer=RpcLineBuffer()
        child_cleaned=False
        while child.poll() is None or selector.get_map():
            if child.poll() is not None and not child_cleaned:
                _terminate_child(child)
                child_cleaned=True
            for key,_ in selector.select(timeout=.05):
                while True:
                    data=os.read(key.fileobj.fileno(),65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        break
                    for line in line_buffer.feed(data):
                        try: event=parse_rpc_line(line)
                        except ValueError: event={"type":"unparsed","content":str(redact(line))}
                        with log_path.open("a",encoding="utf-8") as log: log.write(json.dumps(redact(event),ensure_ascii=False)+"\n")
                        action=runtime.on_event(event)
                        if action and action.get("rpc"):
                            try:
                                child.stdin.write(json.dumps(action["rpc"])+"\n"); child.stdin.flush()
                            except (BrokenPipeError,OSError):
                                continue
                            runtime.commit_response(str(action["question_id"]))
                        elif action: outbox.enqueue(action["event_id"],action)
                        text=_event_text(event)
                        if text:
                            eid="progress-"+hashlib.sha256(text.encode()).hexdigest()[:24]; outbox.enqueue(eid,{"platform":session.platform,"chat":session.chat,"topic":session.topic,"text":text[:4000]})
                    break
            prompts=Outbox(paths.run/f"{session.name}.prompts.json")
            for item in prompts.due():
                try:
                    child.stdin.write(json.dumps({"type":"prompt","id":item.id,"message":str(item.payload["message"])})+"\n"); child.stdin.flush(); prompts.ack(item.id)
                except (BrokenPipeError,OSError): prompts.fail(item.id)
            for event in inbox.poll():
                result=runtime.accept_inbound(event)
                if result.response:
                    try: child.stdin.write(json.dumps(result.response)+"\n"); child.stdin.flush()
                    except (BrokenPipeError,OSError): continue
                    runtime.commit_response(result.question_id, str(event.get("event_id")))
                    inbox.ack(str(event.get("event_id")))
                elif result.terminal: inbox.reject(str(event.get("event_id")))
            for item in outbox.due():
                try: bridge.deliver(item.payload); outbox.ack(item.id)
                except Exception as exc: outbox.fail(item.id,error=str(exc))
        if not child_cleaned:
            _terminate_child(child)
            child_cleaned=True
        selector.close()
        child.stdout.close()
        try:
            child.stdin.close()
        except (BrokenPipeError,OSError):
            pass
        residue=line_buffer.finish()
        if residue:
            try: content=json.dumps(redact(json.loads(residue)),ensure_ascii=False)
            except (json.JSONDecodeError,TypeError): content=str(redact(residue))
            event={"type":"unparsed","content":content}
            with log_path.open("a",encoding="utf-8") as log: log.write(json.dumps(redact(event),ensure_ascii=False)+"\n")
        for item in outbox.due():
            try: bridge.deliver(item.payload); outbox.ack(item.id)
            except Exception as exc: outbox.fail(item.id,error=str(exc))
        code=int(child.returncode or 0)
        session.status="stopped" if stopping else ("completed" if code==0 else "crashed")
        session.last_activity=time.time(); session.supervisor_pid=0; session.omp_pid=0
        store.save(session); terminal_state_saved=True
        return code
    finally:
        original_error=sys.exc_info()[1]
        cleanup_error: Optional[BaseException]=None

        def cleanup(action) -> None:
            nonlocal cleanup_error
            try:
                action()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error=exc

        if child is not None:
            cleanup(lambda: _terminate_child(child))
        if selector is not None:
            cleanup(selector.close)
        if child is not None:
            if child.stdout is not None:
                cleanup(child.stdout.close)
            if child.stdin is not None:
                cleanup(child.stdin.close)
        if child is not None and not terminal_state_saved:
            session.status="crashed"; session.last_activity=time.time(); session.supervisor_pid=0; session.omp_pid=0
            cleanup(lambda: store.save(session))
        cleanup(lambda: release_owner_lock(lock,fd,lock_token))
        if original_error is None and cleanup_error is not None:
            raise cleanup_error


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser(); p.add_argument("name"); args=p.parse_args(argv); return run(args.name)

if __name__=="__main__": raise SystemExit(main())
