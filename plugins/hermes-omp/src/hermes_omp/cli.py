from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
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
from .core import Outbox, Paths, SCHEMA_VERSION, Session, SessionStore, atomic_write, redact, slug, validate_session
from .runtime import inspect_adoption, owner_lock_live, run
from .service import backend_for

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_VALIDATION = 5
ARCHIVE_VERSION = 1


class CliError(Exception):
    def __init__(self, message: str, code: str = "error", exit_code: int = EXIT_ERROR):
        super().__init__(message); self.code = code; self.exit_code = exit_code


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception:
        return "unavailable"


def _json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes omp", description="Durable OMP supervision")
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); _json(doctor); doctor.add_argument("--fix", action="store_true"); doctor.add_argument("--dry-run", action="store_true")
    create = sub.add_parser("create"); create.add_argument("name"); create.add_argument("--cwd", required=True); create.add_argument("--model", required=True); create.add_argument("--mission", required=True); create.add_argument("--project", default=""); create.add_argument("--platform", default=""); create.add_argument("--chat", default=""); create.add_argument("--topic", default=""); create.add_argument("--allowed-user", action="append", default=[]); create.add_argument("--resume", default=""); create.add_argument("--restart-policy", choices=["never", "on-failure", "always"], default="on-failure"); create.add_argument("--omp-path", default="omp"); create.add_argument("--omp-option", action="append", default=[]); create.add_argument("--no-install", action="store_true"); create.add_argument("--start", action="store_true"); create.add_argument("--dry-run", action="store_true"); _json(create)
    adopt = sub.add_parser("adopt"); adopt.add_argument("name"); adopt.add_argument("--inspection", required=True); adopt.add_argument("--mission", required=True); adopt.add_argument("--platform", default=""); adopt.add_argument("--chat", default=""); adopt.add_argument("--topic", default=""); adopt.add_argument("--allowed-user", action="append", default=[]); adopt.add_argument("--restart-policy", choices=["never", "on-failure", "always"], default="on-failure"); adopt.add_argument("--omp-path", default="omp"); adopt.add_argument("--no-install", action="store_true"); adopt.add_argument("--start", action="store_true"); adopt.add_argument("--dry-run", action="store_true"); _json(adopt)
    listing = sub.add_parser("list"); _json(listing)
    status = sub.add_parser("status"); status.add_argument("name"); _json(status)
    send = sub.add_parser("send"); send.add_argument("name"); send.add_argument("message"); _json(send)
    logs = sub.add_parser("logs"); logs.add_argument("name"); logs.add_argument("--lines", type=int, default=100); logs.add_argument("--follow", action="store_true"); logs.add_argument("--since", default=""); logs.add_argument("--level", default=""); logs.add_argument("--poll-interval", type=float, default=0.5); logs.add_argument("--max-polls", type=int, default=0, help=argparse.SUPPRESS); _json(logs)
    events = sub.add_parser("events"); events.add_argument("name"); events.add_argument("--queue", default="prompt,outbound,inbound"); events.add_argument("--status", default=""); events.add_argument("--limit", type=int, default=100); _json(events)
    retry = sub.add_parser("retry"); retry.add_argument("name"); retry.add_argument("event_id", nargs="?"); retry.add_argument("--all", action="store_true"); retry.add_argument("--yes", action="store_true", required=True); _json(retry)
    export = sub.add_parser("export"); export.add_argument("name"); export.add_argument("archive"); _json(export)
    imp = sub.add_parser("import"); imp.add_argument("archive"); imp.add_argument("--conflict", choices=["fail", "rename", "replace"], default="fail"); imp.add_argument("--dry-run", action="store_true"); imp.add_argument("--no-install", action="store_true"); imp.add_argument("--start", action="store_true"); _json(imp)
    update = sub.add_parser("update"); update.add_argument("name"); update.add_argument("--model"); update.add_argument("--mission"); update.add_argument("--platform"); update.add_argument("--chat"); update.add_argument("--topic"); update.add_argument("--allowed-user", action="append"); update.add_argument("--restart-policy", choices=["never", "on-failure", "always"]); update.add_argument("--omp-option", action="append"); update.add_argument("--apply-restart", action="store_true"); update.add_argument("--dry-run", action="store_true"); update.add_argument("--no-install", action="store_true"); _json(update)
    for name in ("stop", "restart"):
        item = sub.add_parser(name); item.add_argument("name"); _json(item)
    remove = sub.add_parser("remove"); remove.add_argument("name"); remove.add_argument("--no-service", action="store_true"); _json(remove)
    config = sub.add_parser("config"); _json(config); config_sub = config.add_subparsers(dest="config_command", required=True); validate = config_sub.add_parser("validate"); validate.add_argument("name"); _json(validate); template = config_sub.add_parser("template"); _json(template)
    completion = sub.add_parser("completion"); completion.add_argument("shell", choices=["bash", "zsh", "fish"]); _json(completion)
    runner = sub.add_parser("run"); runner.add_argument("name")
    inbound = sub.add_parser("inbound"); inbound.add_argument("name"); _json(inbound)
    for field in ("event-id", "question-id", "platform", "chat", "topic", "user", "answer"): inbound.add_argument("--" + field, required=True)
    return p


def _runtime_command(name: str) -> list[str]: return [sys.executable, "-m", "hermes_omp.runtime", name]


def _definition(session: Session, paths: Paths) -> Any:
    value = backend_for(root=paths.root).definition(session.name, _runtime_command(session.name), session.cwd, session.restart_policy)
    return value if isinstance(value, dict) else str(value)


def _install(session: Session, no_install: bool, start: bool, paths: Paths) -> None:
    if no_install: return
    backend = backend_for(root=paths.root); backend.install(session.name, _runtime_command(session.name), session.cwd, session.restart_policy, activate=True)
    if start: backend.start(session.name)


def _rollback_create(session: Session, paths: Paths) -> None:
    try: backend_for(root=paths.root).remove(session.name)
    except Exception: pass
    for target in (paths.sessions / f"{session.name}.json", paths.run / f"{session.name}.omp-path"): target.unlink(missing_ok=True)


def _persist_and_install(session: Session, omp_path: str, no_install: bool, start: bool, paths: Paths) -> None:
    SessionStore(paths).save(session); atomic_write(paths.run / f"{session.name}.omp-path", omp_path + "\n")
    try: _install(session, no_install, start, paths)
    except Exception:
        _rollback_create(session, paths); raise


def _owner_live(paths: Paths, name: str) -> bool:
    return owner_lock_live(paths.run / f"{slug(name)}.owner")


def doctor(paths: Paths, fix: bool = False, dry_run: bool = False) -> dict[str, Any]:
    omp = os.environ.get("HERMES_OMP_BINARY") or shutil.which("omp")
    hermes = os.environ.get("HERMES_OMP_HERMES") or shutil.which("hermes")
    repairs = []
    expected = [paths.root, paths.sessions, paths.run, paths.logs, paths.outbox, paths.inbox, paths.quarantine, paths.services]
    for path in expected:
        if not path.exists():
            repairs.append({"action": "mkdir", "path": str(path), "applied": fix and not dry_run})
            if fix and not dry_run: path.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif stat.S_IMODE(path.stat().st_mode) != 0o700:
            repairs.append({"action": "chmod", "path": str(path), "mode": "0700", "applied": fix and not dry_run})
            if fix and not dry_run: os.chmod(path, 0o700)
    if paths.run.exists():
        for lock in paths.run.glob("*.owner"):
            live = owner_lock_live(lock)
            if not live:
                repairs.append({"action": "remove_stale_lock", "path": str(lock), "applied": fix and not dry_run})
                if fix and not dry_run: lock.unlink(missing_ok=True)
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
    result.update({"health": "degraded" if last_error or queues["outbound_dead"] or queues["prompt_dead"] else ("healthy" if live else "stopped"), "active": live, "queues": queues, "last_error": str(redact(last_error)), "last_activity": session.last_activity})
    return result


def _read_json_files(path: Path, status: str, queue: str) -> list[dict[str, Any]]:
    result = []
    if not path.exists(): return result
    for item in path.glob("*.json"):
        try: payload = json.loads(item.read_text())
        except (OSError, ValueError): payload = {"error": "unreadable"}
        result.append({"queue": queue, "status": status, "id": str(payload.get("event_id") or item.stem), "payload": redact(payload), "created_at": item.stat().st_mtime})
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


def _archive(session: Session, paths: Paths) -> dict[str, Any]:
    data = dataclasses.asdict(session); data["supervisor_pid"] = 0; data["omp_pid"] = 0; data["status"] = "imported"
    omp_path = paths.run / f"{session.name}.omp-path"
    runtime = paths.run / f"{session.name}.runtime.json"
    safe_runtime: dict[str, Any] = {}
    if runtime.exists():
        try:
            raw = json.loads(runtime.read_text()); safe_runtime = {k: redact(v) for k, v in raw.items() if k in {"question", "seen_event_ids"}}
        except ValueError: safe_runtime = {}
    return {"archive_version": ARCHIVE_VERSION, "created_by": __version__, "session": redact(data), "omp_path": omp_path.read_text().strip() if omp_path.exists() else "omp", "runtime": safe_runtime}


def _parse_archive(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text())
    except (OSError, ValueError) as exc: raise CliError("invalid archive JSON", "validation", EXIT_VALIDATION) from exc
    if not isinstance(value, dict) or value.get("archive_version") != ARCHIVE_VERSION or not isinstance(value.get("session"), dict): raise CliError("unsupported or invalid archive schema", "validation", EXIT_VALIDATION)
    allowed = {field.name for field in dataclasses.fields(Session)}
    if set(value["session"]) != allowed: raise CliError("archive session schema mismatch", "validation", EXIT_VALIDATION)
    try: session = Session(**value["session"])
    except (TypeError, ValueError) as exc: raise CliError("invalid session in archive", "validation", EXIT_VALIDATION) from exc
    errors = validate_session(session)
    if errors: raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
    return value


def _completion(shell: str) -> str:
    commands = "doctor create adopt list status send logs events retry export import update stop restart remove config completion"
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
            session = Session.new(name=args.name, cwd=str(cwd), model=args.model, mission=args.mission, project=args.project, platform=args.platform, chat=args.chat, topic=args.topic, allowed_users=args.allowed_user, restart_policy=args.restart_policy, omp_session_id=args.resume, plugin_version=__version__, hermes_version=_version([os.environ.get("HERMES_OMP_HERMES", "hermes"), "--version"]), omp_version=_version([args.omp_path, "--version"]), omp_options=args.omp_option)
        else:
            try: data = json.loads(Path(args.inspection).read_text()); info = inspect_adoption(list(data["argv"]), str(data["cwd"]))
            except (OSError, ValueError, KeyError, TypeError) as exc: raise CliError("invalid adoption inspection", "validation", EXIT_VALIDATION) from exc
            session = Session.new(name=args.name, cwd=info["cwd"], model=info["model"], mission=args.mission, platform=args.platform, chat=args.chat, topic=args.topic, allowed_users=args.allowed_user, restart_policy=args.restart_policy, omp_session_id=info["omp_session_id"], plugin_version=__version__, omp_version="adopted")
        if args.dry_run:
            _emit(args, {"dry_run": True, "session": dataclasses.asdict(session), "service_definition": _definition(session, paths), "would_install": not args.no_install, "would_start": args.start}); return EXIT_OK
        store = SessionStore(paths)
        store.assert_unique_omp_id(session.omp_session_id)
        _persist_and_install(session, args.omp_path, args.no_install, args.start, paths); _emit(args, dataclasses.asdict(session), f"Created {session.name}"); return EXIT_OK
    store = SessionStore(paths)
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
                lines = path.read_text(errors="replace").splitlines() if path.exists() else []
                for line in lines[seen:]:
                    try: value = json.loads(line)
                    except ValueError: value = {"level": "info", "message": line}
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
        session = _load(store, args.name); archive = _archive(session, paths); target = Path(args.archive).expanduser(); atomic_write(target, json.dumps(archive, indent=2, sort_keys=True) + "\n"); _emit(args, {"exported": session.name, "archive": str(target), "archive_version": ARCHIVE_VERSION}, f"Exported {session.name} to {target}"); return EXIT_OK
    if args.command == "import":
        archive = _parse_archive(Path(args.archive)); source = Session(**archive["session"]); name = source.name; existing = paths.sessions / f"{name}.json"
        if existing.exists():
            if args.conflict == "fail": raise CliError(f"session already exists: {name}", "conflict", EXIT_CONFLICT)
            if args.conflict == "rename":
                counter = 2
                while (paths.sessions / f"{name}-{counter}.json").exists(): counter += 1
                name = f"{name}-{counter}"
            elif _owner_live(paths, name): raise CliError("cannot replace an active session", "conflict", EXIT_CONFLICT)
        session_data = dataclasses.asdict(source); session_data.update({"name": name, "id": Session.new(name=name, cwd=source.cwd, model=source.model, mission=source.mission).id, "supervisor_pid": 0, "omp_pid": 0, "status": "imported", "plugin_version": __version__}); session = Session(**session_data)
        plan = {"dry_run": args.dry_run, "name": name, "conflict": args.conflict, "service_definition": _definition(session, paths)}
        if args.dry_run: _emit(args, plan, f"Would import as {name}"); return EXIT_OK
        backups: dict[Path, bytes] = {}
        targets = [paths.sessions / f"{name}.json", paths.run / f"{name}.omp-path", paths.run / f"{name}.runtime.json"]
        for target in targets:
            if target.exists(): backups[target] = target.read_bytes()
        try:
            _persist_and_install(session, str(archive.get("omp_path") or "omp"), args.no_install, args.start, paths)
            runtime = archive.get("runtime") or {}
            if runtime: atomic_write(targets[2], json.dumps(runtime, indent=2) + "\n")
        except Exception:
            for target in targets:
                if target in backups: atomic_write(target, backups[target].decode())
                else: target.unlink(missing_ok=True)
            raise
        _emit(args, {**plan, "imported": name}, f"Imported {name}"); return EXIT_OK
    if args.command == "update":
        session = _load(store, args.name); mutable = {"model": args.model, "mission": args.mission, "platform": args.platform, "chat": args.chat, "topic": args.topic, "allowed_users": args.allowed_user, "restart_policy": args.restart_policy, "omp_options": args.omp_option}; changes = {key: {"from": getattr(session, key), "to": value} for key, value in mutable.items() if value is not None and value != getattr(session, key)}
        if not changes: raise CliError("no mutable changes requested", "validation", EXIT_VALIDATION)
        for key, change in changes.items(): setattr(session, key, change["to"])
        errors = validate_session(session)
        if errors: raise CliError("; ".join(errors), "validation", EXIT_VALIDATION)
        definition = _definition(session, paths); live = _owner_live(paths, session.name)
        if args.dry_run: _emit(args, {"dry_run": True, "changes": changes, "service_definition": definition, "active": live}); return EXIT_OK
        if live and not args.apply_restart: raise CliError("active session requires --apply-restart", "conflict", EXIT_CONFLICT)
        backend = backend_for(root=paths.root); old = _load(store, args.name); old_data = json.dumps(dataclasses.asdict(old), indent=2, sort_keys=True) + "\n"
        try:
            if live: backend.stop(session.name)
            store.save(session)
            if not args.no_install: backend.install(session.name, _runtime_command(session.name), session.cwd, session.restart_policy, activate=True)
            if live: backend.start(session.name)
        except Exception:
            atomic_write(paths.sessions / f"{session.name}.json", old_data)
            try:
                if not args.no_install: backend.install(old.name, _runtime_command(old.name), old.cwd, old.restart_policy, activate=True)
                if live: backend.start(old.name)
            except Exception: pass
            raise
        _emit(args, {"updated": session.name, "changes": changes, "restarted": live}, f"Updated {session.name}"); return EXIT_OK
    if args.command in {"stop", "restart"}:
        _load(store, args.name); backend = backend_for(root=paths.root); backend.stop(args.name)
        if args.command == "restart": backend.start(args.name)
        _emit(args, {"requested": args.command, "name": slug(args.name)}, f"{args.command.title()} requested for {slug(args.name)}"); return EXIT_OK
    if args.command == "remove":
        session = _load(store, args.name); name = session.name; lock = paths.run / f"{name}.owner"
        if _owner_live(paths, name): raise CliError(f"session still running: {name}", "conflict", EXIT_CONFLICT)
        if not args.no_service:
            backend = backend_for(root=paths.root); backend.stop(name); backend.remove(name)
        for target in [paths.sessions / f"{name}.json", paths.run / f"{name}.omp-path", paths.run / f"{name}.runtime.json", paths.run / f"{name}.question.json"]: target.unlink(missing_ok=True)
        lock.unlink(missing_ok=True); _emit(args, {"removed": name}, f"Removed {name}"); return EXIT_OK
    if args.command == "inbound":
        session = _load(store, args.name); event = {key: str(getattr(args, key)) for key in ("event_id", "question_id", "platform", "chat", "topic", "user", "answer")}; FileInbox(paths.inbox / session.name).submit(event); _emit(args, {"queued": True, "event_id": args.event_id, "validation": "runtime"}, f"Queued inbound {args.event_id}"); return EXIT_OK
    if args.command == "config":
        if args.config_command == "validate":
            errors = validate_session(_load(store, args.name)); payload = {"valid": not errors, "errors": errors, "schema_version": SCHEMA_VERSION}; _emit(args, payload, "Valid" if not errors else "Invalid: " + "; ".join(errors)); return EXIT_OK if not errors else EXIT_VALIDATION
        template = {"name": "my-session", "cwd": "/absolute/project/path", "model": "provider/model", "mission": "Describe the mission", "platform": "telegram", "chat": "", "topic": "", "allowed_users": [], "restart_policy": "on-failure", "omp_options": []}; _emit(args, {"template": template, "schema_version": SCHEMA_VERSION}, json.dumps(template, indent=2)); return EXIT_OK
    if args.command == "completion":
        script = _completion(args.shell); _emit(args, {"shell": args.shell, "script": script}, script); return EXIT_OK
    if args.command == "run": return run(args.name, paths=paths)
    return EXIT_USAGE


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command in {"create", "adopt"} and args.dry_run:
            paths = Paths.discover()
        elif args.command == "doctor":
            paths = Paths.discover()
        else:
            paths = Paths.discover(); paths.ensure()
        return _dispatch(args, paths)
    except CliError as exc:
        json_mode = bool(argv and "--json" in argv)
        payload = {"ok": False, "error": {"code": exc.code, "message": str(redact(str(exc)))}}
        if json_mode: print(json.dumps(payload, indent=2, sort_keys=True))
        else: print(f"error: {payload['error']['message']}", file=sys.stderr)
        return exc.exit_code
    except (ValueError, FileNotFoundError) as exc:
        json_mode = bool(argv and "--json" in argv); payload = {"ok": False, "error": {"code": "validation", "message": str(redact(str(exc)))}}
        if json_mode: print(json.dumps(payload, indent=2, sort_keys=True))
        else: print(f"error: {payload['error']['message']}", file=sys.stderr)
        return EXIT_VALIDATION


if __name__ == "__main__": raise SystemExit(main())
