#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

root=Path(os.environ["FAKE_BRIDGE_ROOT"]); root.mkdir(parents=True,exist_ok=True)
if (root/"offline").exists():
    print("offline",file=sys.stderr); raise SystemExit(1)
message=sys.stdin.read()
target=sys.argv[sys.argv.index("--to")+1]
with (root/"delivered.jsonl").open("a") as f: f.write(json.dumps({"target":target,"message":message})+"\n")
