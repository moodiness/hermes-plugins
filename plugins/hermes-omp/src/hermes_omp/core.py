from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = 2
RISKY = re.compile(r"\b(push|publish|release|post|comment|review|merge|deploy|delete|remove|destroy|drop|secret|credential|password|token|permission|payment|purchase|sudo|shell|system command)\b", re.I)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token|password|secret|api[_-]?key)(\s*[=:]\s*)[^\s&\"']+"),
    re.compile(r"(?i)([?&](?:token|key|secret|password)=)[^&\s]+"),
]


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"token|password|secret|authorization|api[_-]?key", str(key), re.I) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: (m.group(1) if m.lastindex == 1 else "".join(m.groups()[:-1])) + "[REDACTED]", result)
    return result


def slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not clean or len(clean) > 63:
        raise ValueError("name must be 1-63 ASCII slug characters")
    return clean


@dataclasses.dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def discover(cls) -> "Paths":
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
        return cls(home / "omp")

    @property
    def sessions(self) -> Path: return self.root / "sessions"
    @property
    def run(self) -> Path: return self.root / "run"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def outbox(self) -> Path: return self.root / "outbox"
    @property
    def inbox(self) -> Path: return self.root / "inbox"
    @property
    def quarantine(self) -> Path: return self.root / "quarantine"
    @property
    def services(self) -> Path: return self.root / "services"

    def ensure(self) -> None:
        for path in (self.root, self.sessions, self.run, self.logs, self.outbox, self.inbox, self.quarantine, self.services):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try: os.chmod(path, 0o700)
            except OSError: pass


@dataclasses.dataclass
class Session:
    schema_version: int
    id: str
    name: str
    omp_session_id: str
    cwd: str
    project: str
    model: str
    omp_options: list[str]
    platform: str
    chat: str
    topic: str
    allowed_users: list[str]
    mission: str
    status: str
    last_activity: float
    supervisor_pid: int
    omp_pid: int
    restart_policy: str
    plugin_version: str
    hermes_version: str
    omp_version: str
    created_at: float

    @classmethod
    def new(cls, *, name: str, cwd: str, model: str, mission: str, platform: str = "", chat: str = "", topic: str = "", restart_policy: str = "on-failure", omp_session_id: str = "", plugin_version: str = "0.1.0rc1", hermes_version: str = "", omp_version: str = "", project: str = "", omp_options: Optional[list[str]] = None, allowed_users: Optional[list[str]] = None) -> "Session":
        name = slug(name)
        now = time.time()
        return cls(SCHEMA_VERSION, hashlib.sha256(f"{name}\0{now}\0{secrets.token_hex(8)}".encode()).hexdigest()[:24], name, omp_session_id, str(Path(cwd).expanduser().resolve()), project or Path(cwd).name, model, omp_options or [], platform, str(chat), str(topic), [str(x) for x in (allowed_users or [])], mission, "created", now, 0, 0, restart_policy, plugin_version, hermes_version, omp_version, now)


class SessionStore:
    def __init__(self, paths: Paths):
        self.paths = paths
        paths.ensure()

    def save(self, session: Session) -> None:
        atomic_write(self.paths.sessions / f"{slug(session.name)}.json", json.dumps(dataclasses.asdict(session), indent=2, sort_keys=True) + "\n")

    def load(self, name: str) -> Session:
        path = self.paths.sessions / f"{slug(name)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            target = self.paths.quarantine / f"{path.stem}.{int(time.time_ns())}.json"
            os.replace(path, target)
            raise ValueError(f"corrupt state quarantined at {target}") from exc
        version = int(data.get("schema_version", 1))
        if version > SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version {version}")
        if version == 1:
            now = time.time()
            defaults = dataclasses.asdict(Session.new(name=data["name"], cwd=data["cwd"], model=data.get("model", ""), mission=data.get("mission", "")))
            defaults.update(data)
            defaults["schema_version"] = SCHEMA_VERSION
            data = defaults
            self.save(Session(**data))
        return Session(**data)

    def list(self) -> list[Session]:
        result = []
        for path in sorted(self.paths.sessions.glob("*.json")):
            result.append(self.load(path.stem))
        return result

    def assert_unique_omp_id(self, omp_session_id: str, except_name: str = "") -> None:
        if not omp_session_id: return
        owners = [s.name for s in self.list() if s.omp_session_id == omp_session_id and s.name != except_name and s.status not in {"removed"}]
        if owners: raise ValueError(f"OMP session ID already owned by {owners[0]}")


def validate_session(session: Session) -> list[str]:
    errors: list[str] = []
    try:
        if slug(session.name) != session.name: errors.append("name must be a canonical slug")
    except ValueError as exc: errors.append(str(exc))
    if session.schema_version != SCHEMA_VERSION: errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not session.id: errors.append("id is required")
    if not Path(session.cwd).expanduser().is_dir(): errors.append("cwd does not exist")
    if not session.model: errors.append("model is required")
    if session.restart_policy not in {"never", "on-failure", "always"}: errors.append("invalid restart_policy")
    if not isinstance(session.omp_options, list) or not all(isinstance(x, str) for x in session.omp_options): errors.append("omp_options must be strings")
    if not isinstance(session.allowed_users, list) or not all(isinstance(x, str) for x in session.allowed_users): errors.append("allowed_users must be strings")
    return errors


@dataclasses.dataclass(frozen=True)
class Option:
    label: str
    description: str = ""
    recommended: bool = False
    reversible: bool = False


@dataclasses.dataclass(frozen=True)
class Question:
    id: str
    session_name: str
    title: str
    method: str
    options: tuple[Option, ...]
    created_at: float
    expires_at: float

    @classmethod
    def from_event(cls, event: dict[str, Any], session_name: str, ttl: float, now: Optional[float] = None) -> "Question":
        qid = str(event.get("id") or "")
        if not qid: raise ValueError("question has no correlation id")
        options = tuple(Option(str(x) if isinstance(x, str) else str(x.get("label") or x.get("title") or x.get("value") or ""), "" if isinstance(x, str) else str(x.get("description") or x.get("detail") or ""), False if isinstance(x, str) else bool(x.get("recommended")), False if isinstance(x, str) else bool(x.get("reversible"))) for x in (event.get("options") or event.get("choices") or []))
        stamp = time.time() if now is None else now
        return cls(qid, slug(session_name), str(event.get("title") or event.get("message") or event.get("question") or "OMP requires input"), str(event.get("method") or "select"), options, stamp, stamp + ttl)

    def message(self) -> str:
        lines = [f"OMP question [{self.id}]", self.title]
        for i, opt in enumerate(self.options, 1):
            marks = " (recommended)" if opt.recommended else ""
            lines.append(f"{i}. {opt.label}{marks}")
            if opt.description: lines.append(f"   {opt.description}")
        lines.append(f"Reply using the public inbound bridge with question_id={self.id}.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Question":
        value=dict(data); value["options"]=tuple(Option(**x) for x in value.get("options",[])); return cls(**value)


def parse_rpc_line(line: str) -> dict[str, Any]:
    try: value = json.loads(line)
    except json.JSONDecodeError as exc: raise ValueError("invalid OMP RPC JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str): raise ValueError("invalid OMP RPC frame")
    return value


def classify_safe_answer(question: Question) -> Optional[str]:
    recommended = [o for o in question.options if o.recommended]
    combined = " ".join([question.title, *(o.label + " " + o.description for o in question.options)])
    if len(recommended) != 1 or not recommended[0].reversible or RISKY.search(combined): return None
    return recommended[0].label


@dataclasses.dataclass(frozen=True)
class Authorization:
    platform: str
    chat: str
    topic: str
    users: tuple[str, ...] = ()

    def authorize(self, event: dict[str, Any], *, expected_question_id: str, seen_event_ids: set[str]) -> bool:
        if not event.get("event_id") or str(event["event_id"]) in seen_event_ids: return False
        if str(event.get("question_id")) != expected_question_id: return False
        if str(event.get("platform", "")) != self.platform or str(event.get("chat", "")) != self.chat or str(event.get("topic", "")) != self.topic: return False
        return not self.users or str(event.get("user", "")) in self.users


@dataclasses.dataclass
class OutboxItem:
    id: str
    payload: dict[str, Any]
    state: str = "pending"
    attempts: int = 0
    next_attempt: float = 0.0
    error: str = ""
    created_at: float = 0.0


class Outbox:
    def __init__(self, path: Path, max_attempts: int = 8, base_delay: float = 2, jitter: Callable[[], float] = lambda: secrets.randbelow(1000) / 1000):
        self.path, self.max_attempts, self.base_delay, self.jitter = path, max_attempts, base_delay, jitter
        self.items: list[OutboxItem] = []
        if path.exists(): self.items = [OutboxItem(**x) for x in json.loads(path.read_text())]

    def _save(self) -> None: atomic_write(self.path, json.dumps([dataclasses.asdict(x) for x in self.items], indent=2) + "\n")
    def enqueue(self, event_id: str, payload: dict[str, Any]) -> bool:
        if any(x.id == event_id for x in self.items): return False
        self.items.append(OutboxItem(event_id, payload, created_at=time.time())); self._save(); return True
    def pending(self) -> list[OutboxItem]: return [x for x in self.items if x.state == "pending"]
    def due(self, now: Optional[float] = None) -> list[OutboxItem]:
        stamp = time.time() if now is None else now
        pending = [x for x in self.items if x.state == "pending"]
        if not pending or pending[0].next_attempt > stamp: return []
        return [pending[0]]
    def ack(self, event_id: str) -> None:
        for x in self.items:
            if x.id == event_id: x.state = "delivered"
        self._save()
    def fail(self, event_id: str, now: Optional[float] = None, error: str = "") -> None:
        stamp = time.time() if now is None else now
        for x in self.items:
            if x.id == event_id:
                x.attempts += 1; x.error = str(redact(error))
                if x.attempts >= self.max_attempts: x.state = "dead"
                else: x.next_attempt = stamp + min(60.0, self.base_delay * (2 ** (x.attempts - 1))) + self.jitter()
        self._save()
    def dead_letters(self) -> list[OutboxItem]: return [x for x in self.items if x.state == "dead"]
    def retry(self, event_id: Optional[str] = None) -> list[str]:
        retried = []
        for item in self.items:
            if item.state == "dead" and (event_id is None or item.id == event_id):
                item.state = "pending"; item.attempts = 0; item.next_attempt = 0.0; item.error = ""; retried.append(item.id)
        if retried: self._save()
        return retried
