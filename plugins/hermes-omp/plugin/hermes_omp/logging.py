from __future__ import annotations

import contextlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .core import redact

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_RECORD_BYTES = 256 * 1024
_ALLOWED_TYPES = {
    "assistant_message", "extension_ui_request", "extension_ui_response",
    "message_end", "turn_end", "error", "warning", "unparsed", "ready",
    "session_start", "session_stop",
}
_DROPPED_TYPES = {"stream_event", "text_delta", "tool_delta", "thinking_delta"}


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 2**31 - 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        number = float(raw)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError
        value = int(number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class LogConfig:
    max_bytes: int = DEFAULT_MAX_BYTES
    backups: int = DEFAULT_BACKUPS
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES

    def __post_init__(self) -> None:
        for name, value in (("max_bytes", self.max_bytes), ("backups", self.backups), ("retention_days", self.retention_days), ("max_record_bytes", self.max_record_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive finite integer")
        if self.max_record_bytes > self.max_bytes:
            raise ValueError("max_record_bytes must not exceed max_bytes")

    @classmethod
    def from_env(cls) -> "LogConfig":
        max_bytes = _positive_int("HERMES_OMP_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
        return cls(
            max_bytes=max_bytes,
            backups=_positive_int("HERMES_OMP_LOG_BACKUPS", DEFAULT_BACKUPS, maximum=100),
            retention_days=_positive_int("HERMES_OMP_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, maximum=36500),
            max_record_bytes=_positive_int("HERMES_OMP_LOG_MAX_RECORD_BYTES", min(DEFAULT_MAX_RECORD_BYTES, max_bytes)),
        )


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(path, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _encode_record(event: dict[str, Any], limit: int) -> bytes:
    value = redact(event)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) <= limit:
        return encoded
    minimal = {
        "type": str(event.get("type", "event"))[:128],
        "timestamp": event.get("timestamp", time.time()),
        "truncated": True,
        "content": "",
    }
    if any(key.lower() in {"token", "secret", "password", "authorization", "api_key"} for key in event):
        minimal["redacted"] = "[REDACTED]"
    overhead = len((json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    content = str(redact(event.get("content", event.get("message", ""))))
    budget = max(0, limit - overhead)
    minimal["content"] = content.encode("utf-8")[:budget].decode("utf-8", "ignore")
    encoded = (json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    while len(encoded) > limit and minimal["content"]:
        minimal["content"] = minimal["content"][:-1]
        encoded = (json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    return encoded


class StructuredLog:
    def __init__(self, path: Path, config: LogConfig | None = None):
        self.path = path
        self.config = config or LogConfig.from_env()
        self.lock_path = path.with_name(path.name + ".lock")

    def write(self, event: dict[str, Any]) -> bool:
        kind = str(event.get("type", ""))
        if kind in _DROPPED_TYPES or (kind and kind not in _ALLOWED_TYPES):
            return False
        record = _encode_record(event, self.config.max_record_bytes)
        with _locked(self.lock_path):
            self.purge_locked()
            size = self.path.stat().st_size if self.path.exists() else 0
            if size and size + len(record) > self.config.max_bytes:
                self.rotate_locked()
            fd = os.open(str(self.path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.chmod(self.path, 0o600)
                os.write(fd, record)
                os.fsync(fd)
            finally:
                os.close(fd)
        return True

    def rotate_locked(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.config.backups}")
        oldest.unlink(missing_ok=True)
        for number in range(self.config.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{number}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{number + 1}"))
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def purge_locked(self, now: float | None = None) -> None:
        cutoff = (time.time() if now is None else now) - self.config.retention_days * 86400
        for item in self.path.parent.glob(self.path.name + ".*"):
            if item == self.lock_path:
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except FileNotFoundError:
                pass

    def remediate_oversized(self) -> bool:
        with _locked(self.lock_path):
            if not self.path.exists() or self.path.stat().st_size <= self.config.max_bytes:
                return False
            self.rotate_locked()
            return True


def log_paths(path: Path, backups: int = DEFAULT_BACKUPS) -> list[Path]:
    return [path.with_name(f"{path.name}.{number}") for number in range(backups, 0, -1)] + [path]


def iter_log_records(path: Path, backups: int = DEFAULT_BACKUPS) -> Iterator[dict[str, Any]]:
    for item in log_paths(path, backups):
        try:
            with item.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        value = {"level": "info", "message": str(redact(line.rstrip("\n")))}
                    if isinstance(value, dict):
                        yield value
        except FileNotFoundError:
            continue


def purge_log_family(path: Path) -> int:
    removed = 0
    for item in path.parent.glob(path.name + "*"):
        try:
            item.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed
