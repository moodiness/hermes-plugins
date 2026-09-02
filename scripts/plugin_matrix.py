#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
catalog = json.loads((root / "plugins.json").read_text())
include = []
for plugin in catalog["plugins"]:
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        for python in ("3.10", "3.11", "3.13"):
            include.append({
                "plugin": plugin["id"],
                "path": plugin["path"],
                "plugin_path": plugin["plugin_path"],
                "os": os_name,
                "python": python,
            })
print(json.dumps({"include": include}, separators=(",", ":")))
