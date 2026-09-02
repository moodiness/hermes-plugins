from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_omp.cli import dashboard_snapshot
from hermes_omp.core import Paths, slug


def _paths() -> Paths:
    return Paths.discover()


def get_snapshot(query: dict[str, Any] | None = None) -> dict[str, Any]:
    limit = int((query or {}).get("log_limit", 20))
    return dashboard_snapshot(_paths(), log_limit=min(100, max(0, limit)))


def post_action(body: dict[str, Any]) -> dict[str, Any]:
    """Return a validated CLI contract; the host executes only after confirmation."""
    if body.get("confirmation") is not True:
        raise ValueError("explicit confirmation is required")
    action = str(body.get("action", ""))
    if action not in {"stop", "restart", "retry"}:
        raise ValueError("unsupported action")
    name = slug(str(body.get("session", "")))
    command = ["hermes", "omp", action, name]
    if action == "retry":
        event_id = str(body.get("event_id", ""))
        if not event_id:
            raise ValueError("retry requires event_id")
        command += [event_id, "--yes"]
    return {"confirmed": True, "command": command, "execute": False}


ROUTES = {("GET", "/snapshot"): get_snapshot, ("POST", "/action"): post_action}
