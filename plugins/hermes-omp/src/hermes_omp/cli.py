from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .bridge import FileInbox
from .core import Outbox, Paths, Session, SessionStore, redact, slug
from .runtime import inspect_adoption, run
from .service import backend_for


def _version(command: list[str]) -> str:
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=10)
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception: return "unavailable"


def build_parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="hermes omp",description="Durable OMP supervision")
    sub=p.add_subparsers(dest="command",required=True)
    doctor=sub.add_parser("doctor"); doctor.add_argument("--json",action="store_true")
    create=sub.add_parser("create"); create.add_argument("name"); create.add_argument("--cwd",required=True); create.add_argument("--model",required=True); create.add_argument("--mission",required=True); create.add_argument("--project",default=""); create.add_argument("--platform",default=""); create.add_argument("--chat",default=""); create.add_argument("--topic",default=""); create.add_argument("--allowed-user",action="append",default=[]); create.add_argument("--resume",default=""); create.add_argument("--restart-policy",choices=["never","on-failure","always"],default="on-failure"); create.add_argument("--omp-path",default="omp"); create.add_argument("--omp-option",action="append",default=[]); create.add_argument("--no-install",action="store_true"); create.add_argument("--start",action="store_true")
    adopt=sub.add_parser("adopt"); adopt.add_argument("name"); adopt.add_argument("--inspection",required=True); adopt.add_argument("--mission",required=True); adopt.add_argument("--platform",default=""); adopt.add_argument("--chat",default=""); adopt.add_argument("--topic",default=""); adopt.add_argument("--allowed-user",action="append",default=[]); adopt.add_argument("--restart-policy",choices=["never","on-failure","always"],default="on-failure"); adopt.add_argument("--omp-path",default="omp"); adopt.add_argument("--no-install",action="store_true"); adopt.add_argument("--start",action="store_true")
    listing=sub.add_parser("list"); listing.add_argument("--json",action="store_true")
    status=sub.add_parser("status"); status.add_argument("name"); status.add_argument("--json",action="store_true")
    send=sub.add_parser("send"); send.add_argument("name"); send.add_argument("message")
    logs=sub.add_parser("logs"); logs.add_argument("name"); logs.add_argument("--lines",type=int,default=100)
    for name in ("stop","restart"):
        x=sub.add_parser(name); x.add_argument("name")
    remove=sub.add_parser("remove"); remove.add_argument("name"); remove.add_argument("--no-service",action="store_true")
    runner=sub.add_parser("run"); runner.add_argument("name")
    inbound=sub.add_parser("inbound"); inbound.add_argument("name")
    for field in ("event-id","question-id","platform","chat","topic","user","answer"): inbound.add_argument("--"+field,required=True)
    return p


def _runtime_command(name: str) -> list[str]:
    return [sys.executable,"-m","hermes_omp.runtime",name]


def _install(session: Session, no_install: bool, start: bool, paths: Paths) -> None:
    if no_install: return
    backend=backend_for(root=paths.root); backend.install(session.name,_runtime_command(session.name),session.cwd,session.restart_policy,activate=True)
    if start: backend.start(session.name)


def doctor(paths: Paths) -> dict[str, Any]:
    omp=os.environ.get("HERMES_OMP_BINARY") or shutil.which("omp")
    hermes=os.environ.get("HERMES_OMP_HERMES") or shutil.which("hermes")
    checks={"omp":{"ok":bool(omp),"path":omp or "","version":_version([omp,"--version"]) if omp else "unavailable"},"hermes_send":{"ok":bool(hermes),"path":hermes or "","version":_version([hermes,"--version"]) if hermes else "unavailable"},"state":{"ok":os.access(paths.root,os.W_OK) if paths.root.exists() else os.access(paths.root.parent,os.W_OK),"path":str(paths.root)},"service_backend":{"ok":True,"name":type(backend_for(root=paths.root)).__name__}}
    return {"ok":all(x["ok"] for x in checks.values()),"plugin_version":__version__,"checks":checks,"state_db_used":False,"telegram_api_used":False,"inbound_contract":"atomic JSON envelopes in $HERMES_HOME/omp/inbox/<session>"}


def main(argv: Optional[list[str]]=None) -> int:
    args=build_parser().parse_args(argv); paths=Paths.discover(); paths.ensure(); store=SessionStore(paths)
    if args.command=="doctor":
        report=doctor(paths); print(json.dumps(report,indent=2)); return 0 if report["ok"] else 1
    if args.command=="create":
        cwd=Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir(): raise ValueError(f"cwd does not exist: {cwd}")
        store.assert_unique_omp_id(args.resume)
        session=Session.new(name=args.name,cwd=str(cwd),model=args.model,mission=args.mission,project=args.project,platform=args.platform,chat=args.chat,topic=args.topic,allowed_users=args.allowed_user,restart_policy=args.restart_policy,omp_session_id=args.resume,plugin_version=__version__,hermes_version=_version([os.environ.get("HERMES_OMP_HERMES","hermes"),"--version"]),omp_version=_version([args.omp_path,"--version"]),omp_options=args.omp_option)
        store.save(session); atomic_path=paths.run/f"{session.name}.omp-path"; atomic_path.write_text(args.omp_path); os.chmod(atomic_path,0o600); _install(session,args.no_install,args.start,paths); print(json.dumps(dataclasses.asdict(session))); return 0
    if args.command=="adopt":
        data=json.loads(Path(args.inspection).read_text()); info=inspect_adoption(list(data["argv"]),str(data["cwd"])); store.assert_unique_omp_id(info["omp_session_id"])
        session=Session.new(name=args.name,cwd=info["cwd"],model=info["model"],mission=args.mission,platform=args.platform,chat=args.chat,topic=args.topic,allowed_users=args.allowed_user,restart_policy=args.restart_policy,omp_session_id=info["omp_session_id"],plugin_version=__version__,omp_version="adopted")
        store.save(session); (paths.run/f"{session.name}.omp-path").write_text(args.omp_path); _install(session,args.no_install,args.start,paths); print(json.dumps(dataclasses.asdict(session))); return 0
    if args.command=="list": print(json.dumps([dataclasses.asdict(x) for x in store.list()],indent=2)); return 0
    if args.command=="status": print(json.dumps(dataclasses.asdict(store.load(args.name)),indent=2)); return 0
    if args.command=="send":
        session=store.load(args.name); out=Outbox(paths.run/f"{session.name}.prompts.json"); eid="prompt-"+hashlib.sha256(f"{time.time_ns()}\0{args.message}".encode()).hexdigest()[:24]; queued=out.enqueue(eid,{"message":args.message}); print(json.dumps({"queued":queued,"event_id":eid})); return 0
    if args.command=="logs":
        path=paths.logs/f"{slug(args.name)}.jsonl"
        if path.exists(): print("\n".join(path.read_text(errors="replace").splitlines()[-args.lines:])); return 0
        return 0
    if args.command in {"stop","restart"}:
        backend=backend_for(root=paths.root); backend.stop(args.name)
        if args.command=="restart": backend.start(args.name)
        print(json.dumps({"requested":args.command,"name":slug(args.name)})); return 0
    if args.command=="remove":
        name=slug(args.name)
        if not args.no_service: backend_for(root=paths.root).remove(name)
        for target in [paths.sessions/f"{name}.json",paths.run/f"{name}.omp-path",paths.run/f"{name}.owner"]:
            if target.exists(): target.unlink()
        print(json.dumps({"removed":name})); return 0
    if args.command=="inbound":
        store.load(args.name); event={key:str(getattr(args,key)) for key in ("event_id","question_id","platform","chat","topic","user","answer")}; FileInbox(paths.inbox/slug(args.name)).submit(event); print(json.dumps({"accepted":True,"event_id":args.event_id})); return 0
    if args.command=="run": return run(args.name,paths=paths)
    return 2

if __name__=="__main__": raise SystemExit(main())
