from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .core import atomic_write, slug


class DeliveryError(RuntimeError):
    pass


class HermesSendBridge:
    """Public outbound adapter: `hermes send`, body exclusively on stdin."""
    def __init__(self, hermes: str = "hermes", runner: Callable[..., Any] = subprocess.run, environ: dict[str, str] | None = None):
        self.hermes, self.runner, self.environ = hermes, runner, environ

    def deliver(self, payload: dict[str, Any]) -> None:
        platform = str(payload["platform"])
        target = platform
        if payload.get("chat"):
            target += ":" + str(payload["chat"])
        if payload.get("topic"):
            target += ":" + str(payload["topic"])
        env = dict(os.environ if self.environ is None else self.environ)
        result = self.runner([self.hermes, "send", "--to", target, "--file", "-", "--quiet"], input=str(payload["text"]), text=True, capture_output=True, env=env, timeout=60)
        if result.returncode:
            raise DeliveryError((result.stderr or "hermes send failed").strip())


class FileInbox:
    """Replaceable inbound adapter contract for a future Hermes message hook.

    Any public gateway/webhook adapter may atomically submit the same JSON
    envelope. The runtime never reads Hermes state or credentials.
    """
    def __init__(self, path: Path):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.done = path / "processed"
        self.done.mkdir(exist_ok=True, mode=0o700)
        self.rejected = path / "rejected"
        self.rejected.mkdir(exist_ok=True, mode=0o700)

    def submit(self, event: dict[str, Any]) -> Path:
        event_id = slug(str(event.get("event_id") or ""))
        target = self.path / f"{event_id}.json"
        atomic_write(target, json.dumps(event, sort_keys=True) + "\n")
        return target

    def poll(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.path.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
            result.append(json.loads(path.read_text()))
        return result

    def ack(self, event_id: str) -> None:
        source = self.path / f"{slug(event_id)}.json"
        if source.exists(): os.replace(source, self.done / source.name)

    def reject(self, event_id: str) -> None:
        source = self.path / f"{slug(event_id)}.json"
        if source.exists(): os.replace(source, self.rejected / source.name)
