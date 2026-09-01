#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

state=Path(os.environ["FAKE_OMP_STATE"])
resume=""
if "--resume" in sys.argv: resume=sys.argv[sys.argv.index("--resume")+1]
if not resume: resume="fake-session-001"
state.write_text(json.dumps({"session_id":resume,"pid":os.getpid(),"started":time.time()}))
for raw in sys.stdin:
    frame=json.loads(raw)
    if frame.get("type")=="negotiate_protocol": print(json.dumps({"type":"protocol_negotiated","sessionId":resume}),flush=True)
    elif frame.get("type")=="prompt":
        print(json.dumps({"type":"message_end","message":{"role":"assistant","content":str(frame.get("message"))+" received"}}),flush=True)
        if frame.get("id")=="initial": print(json.dumps({"type":"extension_ui_request","id":"q-001","method":"select","title":"Choose safe path","options":[{"label":"Proceed","description":"Reversible local operation","recommended":True,"reversible":True},{"label":"Stop","description":"Stop now"}]}),flush=True)
    elif frame.get("type")=="extension_ui_response":
        print(json.dumps({"type":"message_end","message":{"role":"assistant","content":"answer="+str(frame["value"])}}),flush=True)
        print(json.dumps({"type":"turn_end"}),flush=True)
        time.sleep(float(os.environ.get("FAKE_OMP_EXIT_DELAY", "0.05")))
        sys.exit(0)
