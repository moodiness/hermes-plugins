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
import time
import traceback
from pathlib import Path
from typing import Any, NamedTuple, Optional

from .bridge import FileInbox, HermesSendBridge
from .core import Authorization, Outbox, Paths, Question, Session, SessionStore, atomic_write, classify_safe_answer, parse_rpc_line, redact
from .logging import StructuredLog

RESTART_BUDGET_EXIT = 0


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
    def __init__(self, session: Session, paths: Paths, *, omp_path: str, question_ttl: float = 86400, auto_answer_safe: bool = False, started_at: Optional[float] = None, usage_rpc_trustworthy: bool = False, transition_max_bytes: int = 262144):
        self.session, self.paths, self.omp_path = session, paths, omp_path
        self.store = SessionStore(paths); self.question_ttl=question_ttl; self.auto_answer_safe=auto_answer_safe or session.policy_profile in {"balanced", "night"}
        state_path=paths.run/f"{session.name}.runtime.json"
        self.state_path=state_path; state=json.loads(state_path.read_text()) if state_path.exists() else {}
        question=state.get("question")
        self.question: Optional[Question] = Question.from_dict(question) if question else None
        self.seen: set[str] = set(str(x) for x in state.get("seen_event_ids",[]))
        self.auth=Authorization(session.platform,session.chat,session.topic,tuple(session.allowed_users))
        self.started_at=time.time() if started_at is None else started_at
        self.usage_rpc_trustworthy=usage_rpc_trustworthy
        self.transition_max_bytes=max(256, transition_max_bytes)
        self.telemetry_enabled=False
        self.notified=set(str(x) for x in state.get("notified",[]))
        self.launch_attempts=[float(x) for x in state.get("launch_attempts",state.get("restarts",[]))]
        self.restarts=self.launch_attempts
        usage=state.get("usage",{})
        self.usage={"total_tokens":int(usage.get("total_tokens",0)),"cost_usd":float(usage.get("cost_usd",0.0))}
        if started_at is None:
            self.started_at=float(state.get("started_at",self.started_at))

    def _save_state(self) -> None:
        atomic_write(self.state_path,json.dumps({"question":self.question.to_dict() if self.question else None,"seen_event_ids":sorted(self.seen),"notified":sorted(self.notified),"launch_attempts":self.launch_attempts,"restarts":self.launch_attempts,"usage":self.usage,"started_at":self.started_at},indent=2)+"\n")

    def notification(self, kind: str, key: str, text: str) -> Optional[dict[str, Any]]:
        if kind not in self.session.notifications or not self.session.notifications[kind]: return None
        fingerprint=hashlib.sha256(f"{kind}\0{key}".encode()).hexdigest()[:24]
        if fingerprint in self.notified: return None
        return {"event_id":f"notification-{kind}-{fingerprint}","platform":self.session.platform,"chat":self.session.chat,"topic":self.session.topic,"text":str(redact(text))[:4000],"kind":kind,"dedup_key":fingerprint}

    def commit_notification(self, fingerprint: str) -> None:
        self.notified.add(str(fingerprint)); self._save_state()

    def queue_notification(self, fingerprint: str) -> None:
        self.notified.add(str(fingerprint)); self._save_state()

    def restart_status(self, now: Optional[float]=None) -> dict[str, Any]:
        stamp=time.time() if now is None else now
        window=self.session.restart_window_seconds
        recent=[value for value in self.launch_attempts if value <= stamp and (not window or stamp-value <= window)]
        cooldown_remaining=max(0.0, (recent[-1]+self.session.restart_cooldown_seconds-stamp) if recent else 0.0)
        restarts=max(0,len(recent)-1)
        limit_reached=bool(self.session.max_restarts and restarts>=self.session.max_restarts)
        return {"allowed":not limit_reached and cooldown_remaining<=0,"count":restarts,"launch_count":len(recent),"limit":self.session.max_restarts,"window_seconds":window,"cooldown_remaining_seconds":cooldown_remaining}

    def claim_launch(self, now: Optional[float]=None) -> dict[str, Any]:
        stamp=time.time() if now is None else now
        window=self.session.restart_window_seconds
        recent=[value for value in self.launch_attempts if value <= stamp and (not window or stamp-value <= window)]
        cooldown_remaining=max(0.0,(recent[-1]+self.session.restart_cooldown_seconds-stamp) if recent else 0.0)
        restarts=max(0,len(recent)-1)
        allowed=(not self.session.max_restarts or restarts < self.session.max_restarts) and cooldown_remaining <= 0
        status={"allowed":allowed,"count":restarts,"launch_count":len(recent),"limit":self.session.max_restarts,"window_seconds":window,"cooldown_remaining_seconds":cooldown_remaining}
        if allowed:
            recent.append(stamp)
            self.launch_attempts=recent; self.restarts=recent; self._save_state()
        return status

    def record_restart(self, now: Optional[float]=None) -> dict[str, Any]:
        stamp=time.time() if now is None else now
        status=self.restart_status(stamp)
        if status["allowed"]:
            self.launch_attempts=[value for value in self.launch_attempts if value <= stamp and (not self.session.restart_window_seconds or stamp-value<=self.session.restart_window_seconds)]
            self.launch_attempts.append(stamp); self.restarts=self.launch_attempts; self._save_state()
        return status

    def budget_status(self, now: Optional[float]=None) -> dict[str, Any]:
        stamp=time.time() if now is None else now
        elapsed=max(0.0,stamp-self.started_at)
        duration={"state":"unlimited" if not self.session.max_duration_seconds else ("exceeded" if elapsed>self.session.max_duration_seconds else "within_limit"),"elapsed_seconds":elapsed,"limit":self.session.max_duration_seconds}
        unavailable={"state":"unavailable","enforceable":False,"reason":"trustworthy_public_rpc_usage_unavailable"}
        if self.usage_rpc_trustworthy:
            tokens={"state":"unlimited" if not self.session.max_tokens else ("exceeded" if self.usage["total_tokens"]>self.session.max_tokens else "within_limit"),"enforceable":True,"used":self.usage["total_tokens"],"limit":self.session.max_tokens}
            cost={"state":"unlimited" if not self.session.max_cost_usd else ("exceeded" if self.usage["cost_usd"]>self.session.max_cost_usd else "within_limit"),"enforceable":True,"used":self.usage["cost_usd"],"limit":self.session.max_cost_usd}
        else:
            tokens=dict(unavailable) if self.session.max_tokens else {"state":"unlimited","enforceable":False}
            cost=dict(unavailable) if self.session.max_cost_usd else {"state":"unlimited","enforceable":False}
        return {"duration":duration,"tokens":tokens,"cost":cost,"restart":self.restart_status(stamp)}

    def should_start(self) -> bool:
        status=self.budget_status()
        return status["tokens"]["state"]!="unavailable" and status["cost"]["state"]!="unavailable"

    def should_stop(self, now: Optional[float]=None) -> str:
        status=self.budget_status(now)
        if status["duration"]["state"]=="exceeded": return "duration_exceeded"
        if status["tokens"]["state"]=="exceeded": return "token_limit_exceeded"
        if status["cost"]["state"]=="exceeded": return "cost_limit_exceeded"
        return ""

    def transition(self, previous: str, current: str, details: dict[str, Any], *, now: Optional[float]=None) -> None:
        path=self.paths.logs/f"{self.session.name}.transitions.ndjson"; path.parent.mkdir(parents=True,exist_ok=True)
        record={"timestamp":time.time() if now is None else now,"session":self.session.name,"from":previous,"to":current,"reason":str(redact(details.get("reason",current)))[:256],"details":redact(details)}
        line=json.dumps(record,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n"
        if len(line.encode("utf-8")) > self.transition_max_bytes:
            record["details"]={"truncated":True}
            record["reason"]=record["reason"][:32]
            record["truncated"]=True
            line=json.dumps(record,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n"
            if len(line.encode("utf-8")) > self.transition_max_bytes:
                record["reason"]="truncated"
                line=json.dumps(record,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n"
        existing=path.read_text(encoding="utf-8") if path.exists() else ""
        combined=(existing+line).encode("utf-8")
        while len(combined)>self.transition_max_bytes and "\n" in combined.decode("utf-8",errors="ignore"):
            text=combined.decode("utf-8",errors="ignore"); combined=text.split("\n",1)[1].encode("utf-8")
        atomic_write(path,combined)

    def startup_frames(self) -> list[dict[str, Any]]:
        frames=[{"type":"negotiate_protocol","protocolVersion":2,"id":"negotiate"}]
        if self.session.mission: frames.append({"type":"prompt","message":self.session.mission,"id":"initial"})
        return frames

    def _touch(self, now: Optional[float]=None) -> None:
        stamp = time.time() if now is None else now
        self.store.patch(self.session.name, self.session.id, last_activity=stamp)
        self.session.last_activity = stamp

    def on_event(self,event:dict[str,Any],now:Optional[float]=None) -> Optional[dict[str,Any]]:
        self._touch(now)
        if event.get("type")=="usage" and self.usage_rpc_trustworthy and event.get("source")=="public_rpc":
            self.usage={"total_tokens":int(event.get("total_tokens",0)),"cost_usd":float(event.get("cost_usd",0.0))}; self._save_state(); return None
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


def _windows_pid_alive(pid: int, *, kernel32=None, get_last_error=None) -> bool:
    if pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    get_last_error = get_last_error or ctypes.get_last_error
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return get_last_error() != 87  # ERROR_INVALID_PARAMETER means no such PID.
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 0:  # WAIT_OBJECT_0
            return False
        if result == 0x102:  # WAIT_TIMEOUT
            return True
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) calls TerminateProcess on Windows, so use a waitable handle.
        return _windows_pid_alive(pid)
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


def _child_exited_unreaped(child: subprocess.Popen[Any]) -> bool:
    return child.poll() is not None


def _wait_for_unreaped_exit(child: subprocess.Popen[Any], deadline: float) -> bool:
    while True:
        if child.poll() is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _wait_for_group_exit(pgid: int, deadline: float) -> bool:
    while _process_group_alive(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _terminate_child(child: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if getattr(child, "_hermes_omp_cleanup_done", False):
        return

    timeout = max(0.0, timeout)
    if os.name == "nt":
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
        if child.poll() is None:
            child.wait(timeout=max(0.1, timeout))
        setattr(child, "_hermes_omp_cleanup_done", True)
        return

    pgid = child.pid
    exited = child.poll() is not None
    if exited and not _process_group_alive(pgid):
        setattr(child, "_hermes_omp_cleanup_done", True)
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError as exc:
        if not exited:
            raise RuntimeError("supervised child process group disappeared before termination") from exc
    except PermissionError:
        if not exited:
            raise

    term_deadline = time.monotonic() + timeout
    while time.monotonic() < term_deadline and (not exited or _process_group_alive(pgid)):
        if not exited:
            exited = _wait_for_unreaped_exit(child, min(term_deadline, time.monotonic() + 0.01))
        else:
            _wait_for_group_exit(pgid, term_deadline)

    if _process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if not exited:
                raise

    kill_deadline = time.monotonic() + max(0.1, timeout)
    if not exited and not _wait_for_unreaped_exit(child, kill_deadline):
        raise RuntimeError("supervised child did not exit after SIGKILL")

    remaining = max(0.0, kill_deadline - time.monotonic())
    try:
        child.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("supervised child could not be reaped after SIGKILL") from exc

    if not _wait_for_group_exit(pgid, kill_deadline):
        raise RuntimeError("supervised child process group survived SIGKILL")

    setattr(child, "_hermes_omp_cleanup_done", True)


def owner_lock_live(lock: Path, owner: Optional[dict[str,Any]]=None) -> bool:
    if owner is None:
        if not lock.exists():
            return False
        try:
            owner=json.loads(lock.read_text())
        except (OSError,ValueError,TypeError):
            return True
    try:
        if _pid_alive(int(owner.get("pid",0))):
            return True
        orphaned_pid=int(owner.get("orphaned_pid",0))
        orphaned_pgid=int(owner.get("orphaned_pgid",0))
    except (AttributeError,TypeError,ValueError):
        return True
    if orphaned_pid and _pid_alive(orphaned_pid):
        return True
    if orphaned_pgid:
        return _process_group_alive(orphaned_pgid) if os.name != "nt" else True
    return False


def acquire_owner_lock(lock: Path, session_id: str) -> tuple[int,str]:
    token=secrets.token_hex(16); payload=json.dumps({"pid":os.getpid(),"session_id":session_id,"token":token})+"\n"
    while True:
        try: fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            try: current=json.loads(lock.read_text())
            except (OSError,ValueError): raise RuntimeError(f"session owner lock is unreadable: {lock}")
            if owner_lock_live(lock,current):
                if current.get("orphaned_pid") or current.get("orphaned_pgid"):
                    raise RuntimeError("owner lock protects an orphaned child")
                raise RuntimeError("session already owned")
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


def _persist_child_owner(lock: Path, session_id: str, token: str, child_pid: int) -> None:
    marker = "orphaned_pid" if os.name == "nt" else "orphaned_pgid"
    payload={"pid":os.getpid(),"session_id":session_id,"token":token,marker:child_pid}
    atomic_write(lock,json.dumps(payload)+"\n")


def _event_text(event: dict[str,Any]) -> str:
    message=event.get("message")
    if isinstance(message,dict) and message.get("role")=="assistant":
        content=message.get("content","")
        if isinstance(content,str): return content
    return ""


def _add_exception_note(error: BaseException, note: str) -> None:
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)


def run(name: str, *, paths: Optional[Paths]=None, expected_session_id: str="") -> int:
    paths=paths or Paths.discover(); store=SessionStore(paths)
    with store.transaction():
        session=store.load(name)
        if expected_session_id and session.id != expected_session_id:
            raise RuntimeError(f"session identity changed: {session.name}")
        lock=paths.run/f"{session.name}.owner"
        fd,lock_token=acquire_owner_lock(lock,session.id)
        runtime_path = paths.run / f"{name}.omp-path"
        configured_path = runtime_path.read_text().strip() if runtime_path.exists() else ""
        runtime=Runtime(session,paths,omp_path=os.environ.get("HERMES_OMP_BINARY", configured_path or "omp"),auto_answer_safe=os.environ.get("HERMES_OMP_AUTO_ANSWER_SAFE")=="1")
        launch_status=runtime.claim_launch()
        if launch_status["allowed"]:
            session.status="launching"
        else:
            stamp=time.time()
            store.patch(session.name,session.id,status="restart_budget_exceeded",last_activity=stamp,supervisor_pid=0,omp_pid=0)
            runtime.transition(session.status,"restart_budget_exceeded",{"reason":"restart_budget_exceeded","restart":launch_status})
            release_owner_lock(lock,fd,lock_token)
            return RESTART_BUDGET_EXIT
    child: Optional[subprocess.Popen[str]]=None
    selector: Optional[selectors.BaseSelector]=None
    line_buffer: Optional[RpcLineBuffer]=None
    terminal_state_saved=False
    body_error: Optional[BaseException]=None
    stopping=False

    def stop(*_):
        nonlocal stopping
        stopping=True

    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    outbox_path=paths.outbox/f"{session.name}.json"
    outbox=Outbox(outbox_path); inbox=FileInbox(paths.inbox/session.name)
    bridge_environment=dict(os.environ); bridge_environment["HERMES_HOME"]=str(paths.root.parent)
    bridge=HermesSendBridge(hermes=os.environ.get("HERMES_OMP_HERMES","hermes"),environ=bridge_environment)

    if not runtime.should_start():
        store.patch(session.name,session.id,status="budget_unenforceable",last_activity=time.time(),supervisor_pid=0,omp_pid=0)
        release_owner_lock(lock,fd,lock_token)
        raise RuntimeError("configured token/cost cap cannot be enforced: trustworthy public OMP RPC usage is unavailable")

    log_path=paths.logs/f"{session.name}.jsonl"; event_log=StructuredLog(log_path)
    try:
        outbox=Outbox(paths.outbox/f"{session.name}.json")
        for item in outbox.due():
            try: bridge.deliver(item.payload); outbox.ack(item.id)
            except Exception as exc: outbox.fail(item.id,error=str(exc))
        child=subprocess.Popen(build_omp_command(session,runtime.omp_path),cwd=session.cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
        _persist_child_owner(lock,session.id,lock_token,child.pid)
        store.patch(session.name, session.id, status="running", supervisor_pid=os.getpid(), omp_pid=child.pid)
        session.status="running"; session.supervisor_pid=os.getpid(); session.omp_pid=child.pid
        runtime.transition("created","running",{"reason":"process_started","pid":child.pid})
        assert child.stdin and child.stdout
        for frame in runtime.startup_frames(): child.stdin.write(json.dumps(frame)+"\n")
        child.stdin.flush(); selector=selectors.DefaultSelector(); selector.register(child.stdout,selectors.EVENT_READ)
        line_buffer=RpcLineBuffer()
        child_cleaned=False
        while not child_cleaned or selector.get_map():
            budget_reason=runtime.should_stop()
            if budget_reason:
                runtime.transition("running","stopping",{"reason":budget_reason})
                stopping=True
            if not child_cleaned and (stopping or _child_exited_unreaped(child)):
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
                        event_log.write(event)
                        action=runtime.on_event(event)
                        if action and action.get("rpc"):
                            try:
                                child.stdin.write(json.dumps(action["rpc"])+"\n"); child.stdin.flush()
                            except (BrokenPipeError,OSError):
                                continue
                            runtime.commit_response(str(action["question_id"]))
                        elif action:
                            if session.notifications.get("question",True): outbox.enqueue(action["event_id"],action)
                        text=_event_text(event)
                        if text:
                            eid="progress-"+hashlib.sha256(text.encode()).hexdigest()[:24]; notification=runtime.notification("milestone",eid,text)
                            if notification:
                                deliverable={key:value for key,value in notification.items() if key != "dedup_key"}
                                try: bridge.deliver(deliverable); runtime.commit_notification(notification["dedup_key"])
                                except Exception:
                                    if outbox.enqueue(deliverable["event_id"],deliverable): runtime.queue_notification(notification["dedup_key"])
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
            event_log.write(event)
        for item in outbox.due():
            try: bridge.deliver(item.payload); outbox.ack(item.id)
            except Exception as exc: outbox.fail(item.id,error=str(exc))
        code=int(child.returncode or 0)
        budget_stop=bool(runtime.should_stop())
        terminal_status="budget_exceeded" if budget_stop else ("stopped" if stopping else ("completed" if code==0 else "crashed"))
        terminal_activity=time.time()
        store.patch(session.name, session.id, status=terminal_status, last_activity=terminal_activity, supervisor_pid=0, omp_pid=0)
        session.status=terminal_status; session.last_activity=terminal_activity; session.supervisor_pid=0; session.omp_pid=0
        runtime.transition("running",terminal_status,{"reason":"process_exit","code":code})
        kind="completion" if terminal_status=="completed" else "error"
        notification=runtime.notification(kind,f"exit-{code}-{terminal_status}",f"OMP session {session.name} {terminal_status} (exit {code})") if (session.platform and session.chat and not outbox.pending()) else None
        if notification:
            deliverable={key:value for key,value in notification.items() if key != "dedup_key"}
            try: bridge.deliver(deliverable); runtime.commit_notification(notification["dedup_key"])
            except Exception:
                if outbox.enqueue(deliverable["event_id"],deliverable): runtime.queue_notification(notification["dedup_key"])
        terminal_state_saved=True
        if outbox_path.exists() and not outbox.items:
            outbox_path.unlink(missing_ok=True)
        return code
    except BaseException as exc:
        body_error=exc
        raise
    finally:
        termination_error: Optional[BaseException]=None
        cleanup_error: Optional[BaseException]=None

        if child is not None:
            try:
                _terminate_child(child)
            except BaseException as exc:
                termination_error=exc

        def cleanup(action) -> None:
            nonlocal cleanup_error
            try:
                action()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error=exc

        if selector is not None:
            cleanup(selector.close)
        if child is not None:
            if child.stdout is not None:
                cleanup(child.stdout.close)
            if child.stdin is not None:
                cleanup(child.stdin.close)
        if child is not None and not terminal_state_saved:
            crashed_activity=time.time()
            crashed_fields: dict[str, Any] = {"status": "crashed", "last_activity": crashed_activity}
            session.status="crashed"; session.last_activity=crashed_activity
            if termination_error is None:
                crashed_fields.update({"supervisor_pid": 0, "omp_pid": 0})
                session.supervisor_pid=0; session.omp_pid=0
            cleanup(lambda: store.patch(session.name, session.id, **crashed_fields))
        if termination_error is None:
            cleanup(lambda: release_owner_lock(lock,fd,lock_token))
        elif child is not None:
            cleanup(lambda: os.close(fd))
            if body_error is not None:
                _add_exception_note(body_error, f"supervised child cleanup failed: {termination_error}")
            else:
                raise termination_error
        if cleanup_error is not None:
            if body_error is not None:
                _add_exception_note(body_error, f"additional cleanup failed: {cleanup_error}")
            elif termination_error is None:
                raise cleanup_error

def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser(); p.add_argument("name"); p.add_argument("--root",required=True); p.add_argument("--expected-session-id",default=""); p.add_argument("--service-log", default=""); args=p.parse_args(argv)
    try:
        return run(args.name,paths=Paths(Path(args.root)),expected_session_id=args.expected_session_id)
    except BaseException as exc:
        if args.service_log:
            StructuredLog(Path(args.service_log)).write({"type":"error","timestamp":time.time(),"error_type":type(exc).__name__,"message":str(exc),"traceback":traceback.format_exc()})
        raise

if __name__=="__main__": raise SystemExit(main())
