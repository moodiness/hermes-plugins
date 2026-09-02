from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .bridge import FileInbox
from .core import Outbox, Paths, SCHEMA_VERSION, Session, SessionStore, VALID_POLICY_PROFILES, _migrate_abandoned_legacy_lock, atomic_write, redact, slug, validate_session
from .runtime import inspect_adoption, owner_lock_live, run
from .service import backend_for
from .logging import LogConfig, StructuredLog, iter_log_records, purge_log_family

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_VALIDATION = 5
ARCHIVE_VERSION = 1
POLICY_PROFILES = {
    "interactive": {"auto_answer_safe": False, "sensitive": False},
    "balanced": {"auto_answer_safe": True, "sensitive": False},
    "night": {"auto_answer_safe": True, "sensitive": False},
    "strict": {"auto_answer_safe": False, "sensitive": False},
}


class CliError(Exception):
    def __init__(self, message: str, code: str = "error", exit_code: int = EXIT_ERROR):
        super().__init__(message); self.code = code; self.exit_code = exit_code


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception:
        return "unavailable"


def _json(
    parser: argparse.ArgumentParser, *, suppress_default: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_default else False
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="emit stable machine-readable JSON",
    )


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.description = "Durable OMP supervision"
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); _json(doctor); doctor.add_argument("--fix", action="store_true"); doctor.add_argument("--dry-run", action="store_true")
    create = sub.add_parser("create"); create.add_argument("name"); create.add_argument("--cwd", required=True); create.add_argument("--model", required=True); create.add_argument("--mission", required=True); create.add_argument("--project", default=""); create.add_argument("--platform", default=""); create.add_argument("--chat", default=""); create.add_argument("--topic", default=""); create.add_argument("--allowed-user", action="append", default=[]); create.add_argument("--resume", default=""); create.add_argument("--restart-policy", choices=["never", "on-failure", "always"], default="on-failure"); create.add_argument("--policy", choices=VALID_POLICY_PROFILES, default="interactive"); create.add_argument("--omp-path", default="omp"); create.add_argument("--omp-option", action="append", default=[]); create.add_argument("--notify", action="append", choices=["question", "error", "milestone", "completion", "restart"]); create.add_argument("--no-notify", action="append", choices=["question", "error", "milestone", "completion", "restart"], default=[]); create.add_argument("--max-duration", type=float, default=0.0); create.add_argument("--max-restarts", type=int, default=0); create.add_argument("--restart-window", type=float, default=0.0); create.add_argument("--restart-cooldown", type=float, default=0.0); create.add_argument("--max-tokens", type=int, default=0); create.add_argument("--max-cost-usd", type=float, default=0.0); create.add_argument("--no-install", action="store_true"); create.add_argument("--start", action="store_true"); create.add_argument("--dry-run", action="store_true"); _json(create)
    adopt = sub.add_parser("adopt"); adopt.add_argument("name"); adopt.add_argument("--inspection", required=True); adopt.add_argument("--mission", required=True); adopt.add_argument("--platform", default=""); adopt.add_argument("--chat", default=""); adopt.add_argument("--topic", default=""); adopt.add_argument("--allowed-user", action="append", default=[]); adopt.add_argument("--restart-policy", choices=["never", "on-failure", "always"], default="on-failure"); adopt.add_argument("--policy", choices=VALID_POLICY_PROFILES, default="interactive"); adopt.add_argument("--omp-path", default="omp"); adopt.add_argument("--no-install", action="store_true"); adopt.add_argument("--start", action="store_true"); adopt.add_argument("--dry-run", action="store_true"); _json(adopt)
    listing = sub.add_parser("list"); _json(listing)
    status = sub.add_parser("status"); status.add_argument("name"); _json(status)
    send = sub.add_parser("send"); send.add_argument("name"); send.add_argument("message"); _json(send)
    logs = sub.add_parser("logs"); logs.add_argument("name"); logs.add_argument("--lines", type=int, default=100); logs.add_argument("--follow", action="store_true"); logs.add_argument("--since", default=""); logs.add_argument("--level", default=""); logs.add_argument("--poll-interval", type=float, default=0.5); logs.add_argument("--max-polls", type=int, default=0, help=argparse.SUPPRESS); _json(logs)
    events = sub.add_parser("events"); events.add_argument("name"); events.add_argument("--queue", default="prompt,outbound,inbound"); events.add_argument("--status", default=""); events.add_argument("--limit", type=int, default=100); _json(events)
    retry = sub.add_parser("retry"); retry.add_argument("name"); retry.add_argument("event_id", nargs="?"); retry.add_argument("--all", action="store_true"); retry.add_argument("--yes", action="store_true", required=True); _json(retry)
    export = sub.add_parser("export"); export.add_argument("name"); export.add_argument("archive"); export_key = export.add_mutually_exclusive_group(); export_key.add_argument("--hmac-key-file"); export_key.add_argument("--hmac-key-env"); _json(export)
    imp = sub.add_parser("import"); imp.add_argument("archive"); imp.add_argument("--conflict", choices=["fail", "rename", "replace"], default="fail"); imp.add_argument("--dry-run", action="store_true"); imp.add_argument("--no-install", action="store_true"); imp.add_argument("--start", action="store_true"); import_key = imp.add_mutually_exclusive_group(); import_key.add_argument("--hmac-key-file"); import_key.add_argument("--hmac-key-env"); imp.add_argument("--require-signature", action="store_true"); _json(imp)
    update = sub.add_parser("update"); update.add_argument("name"); update.add_argument("--model"); update.add_argument("--mission"); update.add_argument("--platform"); update.add_argument("--chat"); update.add_argument("--topic"); update.add_argument("--allowed-user", action="append"); update.add_argument("--restart-policy", choices=["never", "on-failure", "always"]); update.add_argument("--policy", choices=VALID_POLICY_PROFILES); update.add_argument("--omp-option", action="append"); update.add_argument("--apply-restart", action="store_true"); update.add_argument("--dry-run", action="store_true"); update.add_argument("--no-install", action="store_true"); _json(update)
    migrate = sub.add_parser("migrate-legacy"); migrate.add_argument("name"); migrate.add_argument("--source", "--legacy-file", dest="source"); migrate.add_argument("--apply", action="store_true"); migrate.add_argument("--adopt", action="store_true"); migrate.add_argument("--no-install", action="store_true"); migrate.add_argument("--start", action="store_true"); _json(migrate)
    watch = sub.add_parser("watch"); watch.add_argument("name"); watch.add_argument("--poll-interval", "--interval", dest="poll_interval", type=float, default=1.0); watch.add_argument("--max-polls", type=int, default=0); _json(watch)
    diagnose = sub.add_parser("diagnose"); diagnose.add_argument("name"); diagnose.add_argument("--output"); diagnose.add_argument("--log-lines", type=int, default=100); diagnose.add_argument("--event-limit", type=int, default=100); _json(diagnose)
    clone = sub.add_parser("clone"); clone.add_argument("source"); clone.add_argument("destination", nargs="?", default=""); clone.add_argument("--omp-path"); clone.add_argument("--no-install", action="store_true"); clone.add_argument("--start", action="store_true"); clone.add_argument("--dry-run", action="store_true"); _json(clone)
    for name in ("stop", "restart"):
        item = sub.add_parser(name); item.add_argument("name"); _json(item)
    remove = sub.add_parser("remove"); remove.add_argument("name"); remove.add_argument("--no-service", action="store_true"); remove.add_argument("--purge-logs", action="store_true"); _json(remove)
    config = sub.add_parser("config"); _json(config); config_sub = config.add_subparsers(dest="config_command", required=True); validate = config_sub.add_parser("validate"); validate.add_argument("name"); _json(validate, suppress_default=True); template = config_sub.add_parser("template"); _json(template, suppress_default=True)
    completion = sub.add_parser("completion"); completion.add_argument("shell", choices=["bash", "zsh", "fish"]); _json(completion)
    runner = sub.add_parser("run"); runner.add_argument("name")
    inbound = sub.add_parser("inbound"); inbound.add_argument("name"); _json(inbound)
    for field in ("event-id", "question-id", "platform", "chat", "topic", "user", "answer"): inbound.add_argument("--" + field, required=True)
    return parser


def build_parser() -> argparse.ArgumentParser:
    return configure_parser(argparse.ArgumentParser(prog="hermes omp"))


def _runtime_command(session: Session, paths: Paths) -> list[str]: return [sys.executable, "-m", "hermes_omp.runtime", session.name, "--root", str(paths.root), "--expected-session-id", session.id]


def _definition(session: Session, paths: Paths) -> Any:
    value = backend_for(root=paths.root).definition(session.name, _runtime_command(session, paths), session.cwd, session.restart_policy)
    return value if isinstance(value, dict) else str(value)


def _install(session: Session, no_install: bool, start: bool, paths: Paths) -> None:
    if no_install: return
    backend = backend_for(root=paths.root); backend.install(session.name, _runtime_command(session, paths), session.cwd, session.restart_policy, activate=True)
    if start: backend.start(session.name)


def _restore_file(path: Path, backup: Optional[bytes]) -> None:
    if backup is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, backup)


def _persist_and_install(
    session: Session,
    omp_path: str,
    no_install: bool,
    start: bool,
    paths: Paths,
    *,
    replace: bool = False,
    runtime: Optional[dict[str, Any]] = None,
) -> None:
    store = SessionStore(paths)
    session_path = paths.sessions / f"{session.name}.json"
    omp_path_file = paths.run / f"{session.name}.omp-path"
    runtime_path = paths.run / f"{session.name}.runtime.json"
    targets = [session_path, omp_path_file]
    if runtime is not None:
        targets.append(runtime_path)

    with store.transaction():
        backups = {
            target: target.read_bytes() if target.exists() else None
            for target in targets
        }
        written: list[Path] = []
        service_backend = backend_for(root=paths.root) if not no_install else None
        service_snapshot = service_backend.snapshot(session.name) if service_backend is not None else None
        service_install_attempted = False
        try:
            try:
                if replace:
                    store.replace(session)
                else:
                    store.create(session)
            except FileExistsError as exc:
                raise CliError(f"session already exists: {session.name}", "conflict", EXIT_CONFLICT) from exc
            except ValueError:
                raise
            except Exception:
                _restore_file(session_path, backups[session_path])
                raise
            written.append(session_path)

            written.append(omp_path_file)
            atomic_write(omp_path_file, omp_path + "\n")
            if runtime is not None:
                written.append(runtime_path)
                atomic_write(runtime_path, json.dumps(runtime, indent=2) + "\n")
            if not no_install:
                service_install_attempted = True
                _install(session, no_install, start, paths)
        except Exception:
            for target in reversed(written):
                _restore_file(target, backups[target])
            if service_install_attempted and service_backend is not None and service_snapshot is not None:
                try:
                    service_backend.restore(session.name, service_snapshot)
                except Exception:
                    pass
            raise


def _owner_live(paths: Paths, name: str) -> bool:
    return owner_lock_live(paths.run / f"{slug(name)}.owner")

_OWNER_STOP_TIMEOUT = 5.0


def _wait_owner_stopped(paths: Paths, name: str) -> bool:
    deadline = time.monotonic() + _OWNER_STOP_TIMEOUT
    while _owner_live(paths, name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


def doctor(paths: Paths, fix: bool = False, dry_run: bool = False) -> dict[str, Any]:
    omp = os.environ.get("HERMES_OMP_BINARY") or shutil.which("omp")
    hermes = os.environ.get("HERMES_OMP_HERMES") or shutil.which("hermes")
    repairs = []
    expected = [paths.root, paths.sessions, paths.run, paths.logs, paths.outbox, paths.inbox, paths.quarantine, paths.services]
    for path in expected:
        if not path.exists():
            repairs.append({"action": "mkdir", "path": str(path), "applied": fix and not dry_run})
            if fix and not dry_run: path.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o700:
            repairs.append({"action": "chmod", "path": str(path), "mode": "0700", "applied": fix and not dry_run})
            if fix and not dry_run: os.chmod(path, 0o700)
    live_session_owner = False
    if paths.run.exists():
        for lock in paths.run.glob("*.owner"):
            live = owner_lock_live(lock)
            if live:
                live_session_owner = True
            else:
                repairs.append({"action": "remove_stale_lock", "path": str(lock), "applied": fix and not dry_run})
                if fix and not dry_run: lock.unlink(missing_ok=True)
    for directory in (paths.sessions, paths.run, paths.outbox):
        if not directory.exists():
            continue
        for lock in directory.glob("*.lock"):
            try:
                is_legacy_directory = lock.is_dir() and not lock.is_symlink()
            except OSError:
                continue
            if not is_legacy_directory:
                continue
            repair = {
                "action": "migrate_legacy_path_lock",
                "path": str(lock),
                "applied": False,
            }
            apply_migration = bool(fix and not dry_run and not live_session_owner)
            if _migrate_abandoned_legacy_lock(lock, apply=apply_migration):
                repair["applied"] = apply_migration
                if fix and not dry_run and live_session_owner:
                    repair["reason"] = "live_writer"
            else:
                repair["reason"] = "live_or_unverifiable_owner"
            repairs.append(repair)
    if paths.logs.exists():
        config = LogConfig.from_env()
        for log in paths.logs.glob("*.jsonl"):
            if log.stat().st_size > config.max_bytes:
                name = log.name[:-6]
                live = owner_lock_live(paths.run / f"{name}.owner")
                repair = {"action": "rotate_oversized_log", "path": str(log), "applied": False}
                if live:
                    repair["reason"] = "live_writer"
                elif fix and not dry_run:
                    repair["applied"] = StructuredLog(log, config).remediate_oversized()
                repairs.append(repair)
    probe = paths.root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    parent_writable = os.access(probe, os.W_OK)
    checks = {"omp": {"ok": bool(omp), "path": omp or "", "version": _version([omp, "--version"]) if omp else "unavailable"}, "hermes_send": {"ok": bool(hermes), "path": hermes or "", "version": _version([hermes, "--version"]) if hermes else "unavailable"}, "state": {"ok": parent_writable, "path": str(paths.root)}, "service_backend": {"ok": True, "name": type(backend_for(root=paths.root)).__name__}}
    return {"ok": all(x["ok"] for x in checks.values()), "plugin_version": __version__, "checks": checks, "repairs": repairs, "fix": fix, "dry_run": dry_run, "state_db_used": False, "telegram_api_used": False, "inbound_contract": "atomic JSON envelopes in $HERMES_HOME/omp/inbox/<session>"}


def _load(store: SessionStore, name: str) -> Session:
    try: return store.load(name)
    except FileNotFoundError as exc: raise CliError(f"session not found: {slug(name)}", "not_found", EXIT_NOT_FOUND) from exc


def _queue_summary(paths: Paths, name: str) -> tuple[dict[str, int], str]:
    prompts = Outbox(paths.run / f"{name}.prompts.json")
    outbound = Outbox(paths.outbox / f"{name}.json")
    inbox = paths.inbox / name
    summary = {"prompt_pending": len(prompts.pending()), "prompt_dead": len(prompts.dead_letters()), "outbound_pending": len(outbound.pending()), "outbound_dead": len(outbound.dead_letters()), "inbound_pending": len(list(inbox.glob("*.json"))) if inbox.exists() else 0, "inbound_processed": len(list((inbox / "processed").glob("*.json"))) if (inbox / "processed").exists() else 0, "inbound_rejected": len(list((inbox / "rejected").glob("*.json"))) if (inbox / "rejected").exists() else 0}
    errors = [x.error for x in [*prompts.items, *outbound.items] if x.error]
    return summary, errors[-1] if errors else ""


def _status(session: Session, paths: Paths) -> dict[str, Any]:
    result = dataclasses.asdict(session); queues, last_error = _queue_summary(paths, session.name)
    live = _owner_live(paths, session.name)
    result.update({"health": "degraded" if last_error or queues["outbound_dead"] or queues["prompt_dead"] else ("healthy" if live else "stopped"), "active": live, "queues": queues, "last_error": str(redact(last_error)), "last_activity": session.last_activity, "budgets":_budget_snapshot(session,paths), "notifications":dict(session.notifications)})
    return result


def _budget_snapshot(session: Session, paths: Paths) -> dict[str, Any]:
    state_path=paths.run/f"{session.name}.runtime.json"
    try: state=json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError,ValueError,TypeError): state={}
    elapsed=max(0.0,time.time()-float(state.get("started_at",time.time()))) if state else 0.0
    duration={"state":"unlimited" if not session.max_duration_seconds else ("exceeded" if elapsed>session.max_duration_seconds else "within_limit"),"elapsed_seconds":elapsed,"limit":session.max_duration_seconds}
    unavailable={"state":"unavailable","enforceable":False,"reason":"trustworthy_public_rpc_usage_unavailable"}
    attempts=[]
    for value in state.get("launch_attempts",state.get("restarts",[])):
        try:
            stamp=float(value)
        except (TypeError,ValueError):
            continue
        if math.isfinite(stamp) and stamp <= time.time() and (not session.restart_window_seconds or time.time()-stamp <= session.restart_window_seconds): attempts.append(stamp)
    cooldown=max(0.0,(attempts[-1]+session.restart_cooldown_seconds-time.time()) if attempts else 0.0)
    restart={"allowed":(not session.max_restarts or max(0,len(attempts)-1)<session.max_restarts) and cooldown<=0,"count":max(0,len(attempts)-1),"launch_count":len(attempts),"limit":session.max_restarts,"window_seconds":session.restart_window_seconds,"cooldown_remaining_seconds":cooldown}
    return {"duration":duration,"tokens":dict(unavailable) if session.max_tokens else {"state":"unlimited","enforceable":False},"cost":dict(unavailable) if session.max_cost_usd else {"state":"unlimited","enforceable":False},"restart":restart}


def dashboard_snapshot(paths: Paths, log_limit: int = 20) -> dict[str, Any]:
    store = SessionStore(paths, read_only=True)
    sessions=[]; questions=[]; logs=[]
    for session in store.list():
        sessions.append(redact(_status(session, paths)))
        question=paths.run/f"{session.name}.question.json"
        if question.exists():
            try: questions.append({"session":session.name,**redact(json.loads(question.read_text(encoding="utf-8")))})
            except (OSError,ValueError,TypeError): questions.append({"session":session.name,"error":"unreadable"})
        records=list(iter_log_records(paths.logs/f"{session.name}.jsonl"))[-max(0,log_limit):]
        logs.extend({"session":session.name,"record":redact(record)} for record in records)
    return {"read_only":True,"actions_require_confirmation":True,"sessions":sessions,"questions":questions,"logs":logs[-max(0,log_limit):],"telemetry":False}


def _read_json_files(path: Path, status: str, queue: str) -> list[dict[str, Any]]:
    result = []
    if not path.exists(): return result
    for item in path.glob("*.json"):
        try: payload = json.loads(item.read_text())
        except (OSError, ValueError): payload = {"error": "unreadable"}
        try:
            created_at = item.stat().st_mtime
        except OSError:
            created_at = 0.0
        result.append({"queue": queue, "status": status, "id": str(payload.get("event_id") or item.stem), "payload": redact(payload), "created_at": created_at})
    return result


def _events(paths: Paths, name: str, queues: set[str], statuses: set[str], limit: int) -> list[dict[str, Any]]:
    events = []
    for queue, path in (("prompt", paths.run / f"{name}.prompts.json"), ("outbound", paths.outbox / f"{name}.json")):
        if queue in queues:
            for item in Outbox(path).items: events.append({"queue": queue, "status": item.state, "id": item.id, "payload": redact(item.payload), "attempts": item.attempts, "error": redact(item.error), "created_at": item.created_at})
    if "inbound" in queues:
        root = paths.inbox / name
        events += _read_json_files(root, "pending", "inbound") + _read_json_files(root / "processed", "processed", "inbound") + _read_json_files(root / "rejected", "rejected", "inbound")
    if statuses: events = [event for event in events if event["status"] in statuses]
    return sorted(events, key=lambda x: (float(x.get("created_at", 0)), x["id"]), reverse=True)[:max(0, limit)]


def _legacy_value(record: dict[str, Any], key: str, *containers: str, default: Any = "") -> Any:
    if key in record:
        return record[key]
    for container in containers:
        nested = record.get(container)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return default


def _legacy_source(paths: Paths, source: Optional[str], name: str) -> Path:
    if source:
        return Path(source).expanduser()
    profile = paths.root.parent.resolve()
    candidates = (
        profile / "omp-legacy.json",
        profile / "legacy-omp.json",
        profile / ".hermes-omp-legacy.json",
        paths.root / "legacy.json",
        paths.root / "legacy" / f"{slug(name)}.json",
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(profile)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    raise CliError("no documented legacy record found in selected profile; use --source", "not_found", EXIT_NOT_FOUND)


def _legacy_session(name: str, source: Path, adopt: bool) -> tuple[Session, str]:
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("invalid legacy record", "validation", EXIT_VALIDATION) from exc
    if not isinstance(record, dict):
        raise CliError("invalid legacy record", "validation", EXIT_VALIDATION)
    nested_policy = record.get("policy") if isinstance(record.get("policy"), dict) else {}
    nested_omp = record.get("omp") if isinstance(record.get("omp"), dict) else {}
    resume_identity = str(
        _legacy_value(
            record,
            "omp_session_id",
            "omp",
            "session",
            default=nested_omp.get("session_id", ""),
        )
        or ""
    )
    if adopt and not resume_identity:
        raise CliError(
            "legacy adoption requires a recorded resume identity",
            "validation",
            EXIT_VALIDATION,
        )
    cwd = str(_legacy_value(record, "cwd", "omp", "session", default=""))
    model = str(_legacy_value(record, "model", "omp", "session", default=""))
    mission = str(_legacy_value(record, "mission", "omp", "session", default=""))
    if not cwd:
        raise CliError("legacy record cwd is required", "validation", EXIT_VALIDATION)
    route = record.get("route") if isinstance(record.get("route"), dict) else {}
    allowed = _legacy_value(record, "allowed_users", "route", "routing", default=[])
    options = _legacy_value(record, "omp_options", "session", default=nested_omp.get("options", []))
    if not isinstance(allowed, list) or not isinstance(options, list):
        raise CliError("legacy options and allowed users must be lists", "validation", EXIT_VALIDATION)
    session = Session.new(
        name=name,
        cwd=cwd,
        project=str(_legacy_value(record, "project", "omp", "session", default="")),
        model=model,
        mission=mission,
        platform=str(route.get("platform", _legacy_value(record, "platform", "routing", default=""))),
        chat=str(route.get("chat", _legacy_value(record, "chat", "routing", default=""))),
        topic=str(route.get("topic", _legacy_value(record, "topic", "routing", default=""))),
        allowed_users=[str(value) for value in allowed],
        restart_policy=str(_legacy_value(record, "restart_policy", "omp", "session", default="on-failure")),
        omp_session_id=resume_identity if adopt else "",
        plugin_version=__version__,
        omp_version="legacy",
        omp_options=[str(value) for value in options],
        policy_profile=str(_legacy_value(record, "policy_profile", "session", default=nested_policy.get("profile", "interactive"))),
    )
    errors = validate_session(session)
    if errors:
        raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
    omp_path = str(_legacy_value(record, "omp_path", "session", default=nested_omp.get("path", "omp")))
    return session, omp_path


def _watch(name: str, paths: Paths, interval: float, max_polls: int, as_json: bool) -> None:
    if not math.isfinite(interval) or interval < 0 or max_polls < 0:
        raise CliError("watch interval must be finite and watch values must be non-negative", "validation", EXIT_VALIDATION)
    store = SessionStore(paths)
    previous = ""
    sequence = 0
    observations = 0
    try:
        while not max_polls or observations < max_polls:
            status = redact(_status(_load(store, name), paths))
            fingerprint = json.dumps(status, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            observations += 1
            if fingerprint != previous:
                sequence += 1
                payload = {"sequence": sequence, "status": status}
                if as_json:
                    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False), flush=True)
                else:
                    print(f"{sequence}\t{status['name']}\t{status['health']}\t{status['status']}", flush=True)
                previous = fingerprint
            if max_polls and observations >= max_polls:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def _diagnosis(session: Session, paths: Paths, event_limit: int, log_lines: int) -> dict[str, Any]:
    if event_limit < 0 or log_lines < 0:
        raise CliError("diagnosis limits must be non-negative", "validation", EXIT_VALIDATION)
    queues, last_error = _queue_summary(paths, session.name)
    events = _events(paths, session.name, {"prompt", "outbound", "inbound"}, set(), event_limit)
    logs = list(iter_log_records(paths.logs / f"{session.name}.jsonl"))[-log_lines:] if log_lines else []
    report = {
        "state_db_used": False,
        "telegram_api_used": False,
        "session": dataclasses.asdict(session),
        "status": {"stored": session.status, "queues": queues, "last_error": last_error},
        "events": {"count": len(events), "entries": events},
        "logs": {"count": len(logs), "entries": logs},
    }
    return redact(report)


def _clone_session(source: Session, destination: str) -> Session:
    return Session.new(
        name=destination,
        cwd=source.cwd,
        project=source.project,
        model=source.model,
        mission=source.mission,
        platform=source.platform,
        chat=source.chat,
        topic=source.topic,
        allowed_users=list(source.allowed_users),
        restart_policy=source.restart_policy,
        plugin_version=__version__,
        omp_options=list(source.omp_options),
        policy_profile=source.policy_profile,
    )

def _artifact_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _clone_destination_has_artifacts(paths: Paths, name: str) -> bool:
    queue_paths = (
        paths.run / f"{name}.prompts.json",
        paths.outbox / f"{name}.json",
    )
    artifacts = (
        paths.sessions / f"{name}.json",
        paths.run / f"{name}.omp-path",
        paths.run / f"{name}.runtime.json",
        paths.run / f"{name}.question.json",
        paths.run / f"{name}.owner",
        *queue_paths,
        *(path.with_name(path.name + ".lock") for path in queue_paths),
    )
    if any(_artifact_exists(path) for path in artifacts):
        return True

    inbound = paths.inbox / name
    if inbound.is_symlink() or (inbound.exists() and not inbound.is_dir()):
        return True
    if inbound.is_dir():
        try:
            if next(inbound.iterdir(), None) is not None:
                return True
        except OSError:
            return True

    for pattern in (f"{name}.jsonl*", f"{name}.service.jsonl*"):
        if next(paths.logs.glob(pattern), None) is not None:
            return True
    return False


def _archive(session: Session, paths: Paths) -> dict[str, Any]:
    data = dataclasses.asdict(session); data["supervisor_pid"] = 0; data["omp_pid"] = 0; data["status"] = "imported"
    omp_path = paths.run / f"{session.name}.omp-path"
    runtime = paths.run / f"{session.name}.runtime.json"
    safe_runtime: dict[str, Any] = {}
    if runtime.exists():
        try:
            raw = json.loads(runtime.read_text()); safe_runtime = {k: redact(v) for k, v in raw.items() if k in {"question", "seen_event_ids"}}
        except ValueError: safe_runtime = {}
    return {"archive_version": ARCHIVE_VERSION, "created_by": __version__, "session": data, "omp_path": omp_path.read_text().strip() if omp_path.exists() else "omp", "runtime": safe_runtime}


def _load_hmac_key(args: argparse.Namespace) -> Optional[bytes]:
    key_file = getattr(args, "hmac_key_file", None)
    key_env = getattr(args, "hmac_key_env", None)
    supplied = getattr(args, "_supplied_options", None)
    if supplied is not None and "--hmac-key-file" in supplied and not key_file:
        raise CliError("HMAC key file reference is empty", "validation", EXIT_VALIDATION)
    if supplied is not None and "--hmac-key-env" in supplied and not key_env:
        raise CliError("HMAC key environment reference is empty", "validation", EXIT_VALIDATION)
    if supplied is None and key_file == "":
        raise CliError("HMAC key file reference is empty", "validation", EXIT_VALIDATION)
    if supplied is None and key_env == "":
        raise CliError("HMAC key environment reference is empty", "validation", EXIT_VALIDATION)
    if key_file and key_env:
        raise CliError("specify at most one HMAC key reference", "validation", EXIT_VALIDATION)
    if key_file:
        try:
            key = Path(key_file).expanduser().read_bytes()
        except OSError as exc:
            raise CliError("unable to read HMAC key file", "validation", EXIT_VALIDATION) from exc
    elif key_env:
        value = os.environ.get(key_env)
        key = value.encode("utf-8") if value is not None else b""
    else:
        return None
    if not key:
        raise CliError("HMAC key is missing or empty", "validation", EXIT_VALIDATION)
    return key


def _canonical_archive(archive: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in archive.items() if key != "integrity"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign_archive(archive: dict[str, Any], key: bytes) -> None:
    archive["integrity"] = {
        "algorithm": "hmac-sha256",
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "digest": hmac.new(key, _canonical_archive(archive), hashlib.sha256).hexdigest(),
    }


def _verify_archive(archive: dict[str, Any], key: Optional[bytes], require_signature: bool) -> None:
    if "integrity" not in archive:
        if require_signature:
            raise CliError("archive signature is required", "validation", EXIT_VALIDATION)
        return
    integrity = archive["integrity"]
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "key_id", "digest"}:
        raise CliError("unsupported archive integrity schema", "validation", EXIT_VALIDATION)
    algorithm, key_id, digest = integrity.get("algorithm"), integrity.get("key_id"), integrity.get("digest")
    if algorithm != "hmac-sha256" or not isinstance(key_id, str) or len(key_id) != 16 or any(character not in "0123456789abcdef" for character in key_id) or not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise CliError("unsupported archive integrity schema", "validation", EXIT_VALIDATION)
    if key is None:
        raise CliError("signed archive requires an HMAC key reference", "validation", EXIT_VALIDATION)
    expected_key_id = hashlib.sha256(key).hexdigest()[:16]
    expected_digest = hmac.new(key, _canonical_archive(archive), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(key_id, expected_key_id) or not hmac.compare_digest(digest, expected_digest):
        raise CliError("archive signature verification failed", "validation", EXIT_VALIDATION)


def _read_archive(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text())
    except (OSError, ValueError) as exc: raise CliError("invalid archive JSON", "validation", EXIT_VALIDATION) from exc
    if not isinstance(value, dict): raise CliError("unsupported or invalid archive schema", "validation", EXIT_VALIDATION)
    return value


def _parse_archive(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("archive_version") != ARCHIVE_VERSION or not isinstance(value.get("session"), dict): raise CliError("unsupported or invalid archive schema", "validation", EXIT_VALIDATION)
    allowed = {field.name for field in dataclasses.fields(Session)}
    session_data = dict(value["session"])
    if set(session_data) not in (allowed, allowed - {"policy_profile"}): raise CliError("archive session schema mismatch", "validation", EXIT_VALIDATION)
    session_data.setdefault("policy_profile", "interactive")
    try: session = Session(**session_data)
    except (TypeError, ValueError) as exc: raise CliError("invalid session in archive", "validation", EXIT_VALIDATION) from exc
    errors = validate_session(session)
    if errors: raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
    value["session"] = session_data
    return value


def _completion(shell: str) -> str:
    commands = "doctor create adopt list status send logs events retry export import update stop restart remove config completion migrate-legacy watch diagnose clone"
    if shell == "bash": return f"_hermes_omp() {{ COMPREPLY=($(compgen -W '{commands}' -- \"${{COMP_WORDS[1]}}\")); }}\ncomplete -F _hermes_omp hermes-omp\n"
    if shell == "zsh": return f"#compdef hermes-omp\n_arguments '1:command:({commands})'\n"
    return f"complete -c hermes-omp -f -n '__fish_use_subcommand' -a '{commands}'\n"



def _emit(args: argparse.Namespace, payload: Any, text: Optional[str] = None) -> None:
    if getattr(args, "json", False): print(json.dumps(redact(payload), indent=2, sort_keys=True))
    elif text is not None: print(text)
    elif isinstance(payload, (dict, list)): print(json.dumps(redact(payload), indent=2, sort_keys=True))
    else: print(payload)


def _dispatch(args: argparse.Namespace, paths: Paths) -> int:
    if args.command == "doctor":
        report = doctor(paths, args.fix, args.dry_run); _emit(args, report, "OMP doctor: " + ("ok" if report["ok"] else "issues found")); return EXIT_OK if report["ok"] else EXIT_ERROR
    if args.command in {"create", "adopt"}:
        if args.command == "create":
            cwd = Path(args.cwd).expanduser().resolve()
            if not cwd.is_dir(): raise CliError(f"cwd does not exist: {cwd}", "validation", EXIT_VALIDATION)
            notifications={kind:kind not in args.no_notify for kind in ("question","error","milestone","completion","restart")}
            session = Session.new(name=args.name, cwd=str(cwd), model=args.model, mission=args.mission, project=args.project, platform=args.platform, chat=args.chat, topic=args.topic, allowed_users=args.allowed_user, restart_policy=args.restart_policy, omp_session_id=args.resume, plugin_version=__version__, hermes_version=_version([os.environ.get("HERMES_OMP_HERMES", "hermes"), "--version"]), omp_version=_version([args.omp_path, "--version"]), omp_options=args.omp_option, policy_profile=args.policy, notifications=notifications, max_duration_seconds=args.max_duration, max_restarts=args.max_restarts, restart_window_seconds=args.restart_window, restart_cooldown_seconds=args.restart_cooldown, max_tokens=args.max_tokens, max_cost_usd=args.max_cost_usd)
            errors=validate_session(session)
            if errors: raise CliError("; ".join(errors),"validation",EXIT_VALIDATION)
        else:
            try: data = json.loads(Path(args.inspection).read_text()); info = inspect_adoption(list(data["argv"]), str(data["cwd"]))
            except (OSError, ValueError, KeyError, TypeError) as exc: raise CliError("invalid adoption inspection", "validation", EXIT_VALIDATION) from exc
            session = Session.new(name=args.name, cwd=info["cwd"], model=info["model"], mission=args.mission, platform=args.platform, chat=args.chat, topic=args.topic, allowed_users=args.allowed_user, restart_policy=args.restart_policy, omp_session_id=info["omp_session_id"], plugin_version=__version__, omp_version="adopted", policy_profile=args.policy)
        if args.dry_run:
            _emit(args, {"dry_run": True, "session": dataclasses.asdict(session), "service_definition": _definition(session, paths), "would_install": not args.no_install, "would_start": args.start}); return EXIT_OK
        _persist_and_install(session, args.omp_path, args.no_install, args.start, paths); _emit(args, dataclasses.asdict(session), f"Created {session.name}"); return EXIT_OK
    if args.command == "migrate-legacy":
        source_path = _legacy_source(paths, args.source, args.name)
        session, omp_path = _legacy_session(args.name, source_path, args.adopt)
        plan = {
            "dry_run": not args.apply,
            "applied": bool(args.apply),
            "adopted": bool(args.adopt),
            "session": dataclasses.asdict(session),
            "would_install": not args.no_install,
            "would_start": args.start,
        }
        if not args.apply:
            _emit(args, plan, f"Would migrate {session.name}")
            return EXIT_OK
        _persist_and_install(session, omp_path, args.no_install, args.start, paths)
        _emit(args, plan, f"Migrated {session.name}")
        return EXIT_OK
    if args.command == "import":
        key = _load_hmac_key(args)
        archive = _read_archive(Path(args.archive).expanduser())
        _verify_archive(archive, key, args.require_signature)
        archive = _parse_archive(archive)
        source = Session(**archive["session"])
        store = SessionStore(paths)
        with store.transaction():
            name = source.name
            existing = paths.sessions / f"{name}.json"
            if existing.exists():
                if args.conflict == "fail":
                    raise CliError(f"session already exists: {name}", "conflict", EXIT_CONFLICT)
                if args.conflict == "rename":
                    counter = 2
                    while (paths.sessions / f"{name}-{counter}.json").exists():
                        counter += 1
                    name = f"{name}-{counter}"
                elif _owner_live(paths, name):
                    raise CliError("cannot replace an active session", "conflict", EXIT_CONFLICT)
            replacing = existing.exists() and args.conflict == "replace"
            session_data = dataclasses.asdict(source)
            session_data.update({
                "name": name,
                "id": Session.new(name=name, cwd=source.cwd, model=source.model, mission=source.mission).id,
                "supervisor_pid": 0,
                "omp_pid": 0,
                "status": "imported",
                "plugin_version": __version__,
            })
            session = Session(**session_data)
            plan = {
                "dry_run": args.dry_run,
                "name": name,
                "conflict": args.conflict,
                "service_definition": _definition(session, paths),
            }
            if args.dry_run:
                _emit(args, plan, f"Would import as {name}")
                return EXIT_OK
            runtime = archive.get("runtime") or None
            _persist_and_install(session, str(archive.get("omp_path") or "omp"), args.no_install, args.start, paths, replace=replacing, runtime=runtime)
        _emit(args, {**plan, "imported": name}, f"Imported {name}")
        return EXIT_OK
    if args.command == "watch":
        _watch(args.name, paths, args.poll_interval, args.max_polls, args.json)
        return EXIT_OK
    store = SessionStore(
        paths, read_only=args.command == "clone" and args.dry_run
    )
    if args.command == "diagnose":
        session = _load(store, args.name)
        report = _diagnosis(session, paths, args.event_limit, args.log_lines)
        if args.output:
            target = Path(args.output).expanduser()
            try:
                atomic_write(target, json.dumps(report, indent=2, sort_keys=True) + "\n", mode=0o600)
            except OSError as exc:
                raise CliError("unable to write diagnosis report") from exc
        _emit(args, report, f"Diagnosed {session.name}" + (f" to {target}" if args.output else ""))
        return EXIT_OK
    if args.command == "clone":
        source = _load(store, args.source)
        errors = validate_session(source)
        if errors:
            raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
        if not args.destination:
            raise CliError("clone destination is required", "validation", EXIT_VALIDATION)
        session = _clone_session(source, args.destination)
        if _clone_destination_has_artifacts(paths, session.name):
            raise CliError("clone destination has residual artifacts", "conflict", EXIT_CONFLICT)
        stored_path = paths.run / f"{source.name}.omp-path"
        omp_path = args.omp_path if args.omp_path is not None else (stored_path.read_text().strip() if stored_path.exists() else "omp")
        plan = {
            "dry_run": args.dry_run,
            "source": source.name,
            "destination": session.name,
            "session": dataclasses.asdict(session),
            "would_install": not args.no_install,
            "would_start": args.start,
        }
        if args.dry_run:
            _emit(args, plan, f"Would clone {source.name} to {session.name}")
            return EXIT_OK
        _persist_and_install(session, omp_path, args.no_install, args.start, paths)
        _emit(args, {**plan, "cloned": session.name}, f"Cloned {source.name} to {session.name}")
        return EXIT_OK
    if args.command == "list":
        sessions = [_status(x, paths) for x in store.list()]; _emit(args, {"sessions": sessions}, "\n".join(f"{x['name']}\t{x['health']}\t{x['status']}" for x in sessions) or "No sessions"); return EXIT_OK
    if args.command == "status":
        value = _status(_load(store, args.name), paths); _emit(args, value, f"{value['name']}: {value['health']} ({value['status']})"); return EXIT_OK
    if args.command == "send":
        session = _load(store, args.name); out = Outbox(paths.run / f"{session.name}.prompts.json"); eid = "prompt-" + hashlib.sha256(f"{time.time_ns()}\0{args.message}".encode()).hexdigest()[:24]; queued = out.enqueue(eid, {"message": args.message}); _emit(args, {"queued": queued, "event_id": eid}, f"Queued {eid}"); return EXIT_OK
    if args.command == "events":
        session = _load(store, args.name); queues = {x.strip() for x in args.queue.split(",") if x.strip()}; statuses = {x.strip() for x in args.status.split(",") if x.strip()}; allowed = {"prompt", "outbound", "inbound"}
        if not queues <= allowed: raise CliError("invalid queue filter", "validation", EXIT_VALIDATION)
        events = _events(paths, session.name, queues, statuses, args.limit); _emit(args, {"events": events, "count": len(events)}, "\n".join(f"{x['queue']} {x['status']} {x['id']}" for x in events) or "No events"); return EXIT_OK
    if args.command == "retry":
        session = _load(store, args.name)
        if bool(args.event_id) == bool(args.all): raise CliError("specify one event ID or --all", "validation", EXIT_VALIDATION)
        retried = Outbox(paths.outbox / f"{session.name}.json").retry(None if args.all else args.event_id); _emit(args, {"retried": retried, "count": len(retried), "authorization_bypassed": False}, f"Retried {len(retried)} outbound item(s)"); return EXIT_OK
    if args.command == "logs":
        _load(store, args.name); path = paths.logs / f"{slug(args.name)}.jsonl"; since = float(args.since) if args.since else 0.0; entries: list[Any] = []; seen = 0; polls = 0
        try:
            while True:
                lines = list(iter_log_records(path))
                for value in lines[seen:]:
                    stamp = float(value.get("timestamp", value.get("time", 0)) or 0)
                    if stamp >= since and (not args.level or str(value.get("level", "")).lower() == args.level.lower()): entries.append(redact(value))
                seen = len(lines)
                if not args.follow: break
                polls += 1
                if args.max_polls and polls >= args.max_polls: break
                time.sleep(max(0, args.poll_interval))
        except KeyboardInterrupt: pass
        entries = entries[-max(0, args.lines):]; _emit(args, {"entries": entries, "count": len(entries), "follow": args.follow, "polls": polls}, "\n".join(json.dumps(x, ensure_ascii=False) for x in entries)); return EXIT_OK
    if args.command == "export":
        session = _load(store, args.name)
        archive = _archive(session, paths)
        key = _load_hmac_key(args)
        if key is not None:
            _sign_archive(archive, key)
        target = Path(args.archive).expanduser()
        atomic_write(target, json.dumps(archive, indent=2, sort_keys=True) + "\n")
        _emit(args, {"exported": session.name, "archive": str(target), "archive_version": ARCHIVE_VERSION, "signed": key is not None}, f"Exported {session.name} to {target}")
        return EXIT_OK
    if args.command == "update":
        mutable = {"model": args.model, "mission": args.mission, "platform": args.platform, "chat": args.chat, "topic": args.topic, "allowed_users": args.allowed_user, "restart_policy": args.restart_policy, "policy_profile": args.policy, "omp_options": args.omp_option}
        with store.transaction():
            initial = _load(store, args.name)
            changes = {key: {"from": getattr(initial, key), "to": value} for key, value in mutable.items() if value is not None and value != getattr(initial, key)}
            if not changes:
                raise CliError("no mutable changes requested", "validation", EXIT_VALIDATION)
            proposed = Session(**dataclasses.asdict(initial))
            for key, change in changes.items():
                setattr(proposed, key, change["to"])
            errors = validate_session(proposed)
            if errors:
                raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
            definition = _definition(proposed, paths)
            initially_live = _owner_live(paths, proposed.name)
            if args.dry_run:
                _emit(args, {"dry_run": True, "changes": changes, "service_definition": definition, "active": initially_live})
                return EXIT_OK
            if initially_live and not args.apply_restart:
                raise CliError("active session requires --apply-restart", "conflict", EXIT_CONFLICT)
            expected_id = initial.id

        backend = backend_for(root=paths.root)
        if initially_live:
            backend.stop(initial.name)
            if not _wait_owner_stopped(paths, initial.name):
                with store.transaction():
                    try:
                        current = _load(store, initial.name)
                        if current.id == expected_id:
                            backend.start(initial.name)
                    except Exception:
                        pass
                raise CliError("session did not stop before update", "conflict", EXIT_CONFLICT)

        with store.transaction():
            before_apply: Optional[Session] = None
            service_snapshot = None
            session_written = False
            service_install_attempted = False
            try:
                current = _load(store, args.name)
                if current.id != expected_id:
                    raise CliError("session identity changed during update", "conflict", EXIT_CONFLICT)
                if _owner_live(paths, current.name):
                    message = "session became active during update" if not initially_live else "session did not stop before update"
                    raise CliError(message, "conflict", EXIT_CONFLICT)
                before_apply = Session(**dataclasses.asdict(current))
                for key, change in changes.items():
                    setattr(current, key, change["to"])
                errors = validate_session(current)
                if errors:
                    raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)

                service_snapshot = backend.snapshot(current.name) if not args.no_install else None
                session_path = paths.sessions / f"{current.name}.json"
                session_written = True
                store.save(current)
                if not args.no_install:
                    service_install_attempted = True
                    backend.install(current.name, _runtime_command(current, paths), current.cwd, current.restart_policy, activate=True)
                if initially_live:
                    backend.start(current.name)
            except Exception:
                if before_apply is not None and session_written:
                    store.save(before_apply)
                if service_install_attempted and service_snapshot is not None:
                    try:
                        backend.restore(current.name, service_snapshot)
                    except Exception:
                        pass
                if initially_live:
                    try:
                        latest = _load(store, initial.name)
                        if latest.id == expected_id and not _owner_live(paths, latest.name):
                            backend.start(initial.name)
                    except Exception:
                        pass
                raise
        _emit(args, {"updated": current.name, "changes": changes, "restarted": initially_live}, f"Updated {current.name}")
        return EXIT_OK
    if args.command in {"stop", "restart"}:
        _load(store, args.name); backend = backend_for(root=paths.root); backend.stop(args.name)
        if args.command == "restart": backend.start(args.name)
        _emit(args, {"requested": args.command, "name": slug(args.name)}, f"{args.command.title()} requested for {slug(args.name)}"); return EXIT_OK
    if args.command == "remove":
        with store.transaction():
            session = _load(store, args.name)
            name = session.name
            lock = paths.run / f"{name}.owner"
            if _owner_live(paths, name):
                raise CliError(f"session still running: {name}", "conflict", EXIT_CONFLICT)
            if not args.no_service:
                backend = backend_for(root=paths.root)
                backend.stop(name)
                backend.remove(name)
            for target in [paths.sessions / f"{name}.json", paths.run / f"{name}.omp-path", paths.run / f"{name}.runtime.json", paths.run / f"{name}.question.json"]:
                target.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
            purged_logs = purge_log_family(paths.logs / f"{name}.jsonl") if args.purge_logs else 0
        _emit(args, {"removed": name, "logs_purged": purged_logs, "logs_retained": not args.purge_logs}, f"Removed {name}")
        return EXIT_OK
    if args.command == "inbound":
        session = _load(store, args.name); event = {key: str(getattr(args, key)) for key in ("event_id", "question_id", "platform", "chat", "topic", "user", "answer")}; FileInbox(paths.inbox / session.name).submit(event); _emit(args, {"queued": True, "event_id": args.event_id, "validation": "runtime"}, f"Queued inbound {args.event_id}"); return EXIT_OK
    if args.command == "config":
        if args.config_command == "validate":
            errors = validate_session(_load(store, args.name)); payload = {"valid": not errors, "errors": errors, "schema_version": SCHEMA_VERSION}; _emit(args, payload, "Valid" if not errors else "Invalid: " + "; ".join(errors)); return EXIT_OK if not errors else EXIT_VALIDATION
        template = {"name": "my-session", "cwd": "/absolute/project/path", "model": "provider/model", "mission": "Describe the mission", "platform": "telegram", "chat": "", "topic": "", "allowed_users": [], "restart_policy": "on-failure", "policy_profile": "interactive", "omp_options": []}; _emit(args, {"template": template, "schema_version": SCHEMA_VERSION}, json.dumps(template, indent=2)); return EXIT_OK
    if args.command == "completion":
        script = _completion(args.shell); _emit(args, {"shell": args.shell, "script": script}, script); return EXIT_OK
    if args.command == "run": return run(args.name, paths=paths)
    return EXIT_USAGE


def dispatch_namespace(
    args: argparse.Namespace, paths: Optional[Paths] = None
) -> int:
    try:
        active_paths = paths if paths is not None else Paths.discover()
        read_only_before_dispatch = (
            args.command == "import"
            or args.command == "doctor"
            or (args.command in {"create", "adopt", "clone"} and args.dry_run)
            or (args.command == "migrate-legacy" and not args.apply)
        )
        if not read_only_before_dispatch:
            active_paths.ensure()
        return _dispatch(args, active_paths)
    except CliError as exc:
        payload = {
            "ok": False,
            "error": {"code": exc.code, "message": str(redact(str(exc)))},
        }
        if getattr(args, "json", False):
            if args.command == "watch":
                print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {payload['error']['message']}", file=sys.stderr)
        return exc.exit_code
    except (ValueError, FileNotFoundError) as exc:
        payload = {
            "ok": False,
            "error": {
                "code": "validation",
                "message": str(redact(str(exc))),
            },
        }
        if getattr(args, "json", False):
            if args.command == "watch":
                print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)
            else:
                print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {payload['error']['message']}", file=sys.stderr)
        return EXIT_VALIDATION


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    args._supplied_options = tuple(value for value in raw if value.startswith("--"))
    return dispatch_namespace(args)


if __name__ == "__main__": raise SystemExit(main())
