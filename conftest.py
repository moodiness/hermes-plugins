from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PLUGIN_SOURCE = ROOT / "plugins/hermes-omp/src"
source = str(PLUGIN_SOURCE)
if source in sys.path:
    sys.path.remove(source)
sys.path.insert(0, source)
