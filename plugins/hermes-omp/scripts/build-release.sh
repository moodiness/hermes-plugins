#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${PYTHON:=python3}"
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --format=%ct -- . 2>/dev/null || true)"
fi
: "${SOURCE_DATE_EPOCH:=0}"
export SOURCE_DATE_EPOCH
rm -rf build dist
"$PYTHON" -m build --no-isolation
"$PYTHON" - "$SOURCE_DATE_EPOCH" <<'PY'
from pathlib import Path
import os, sys, zipfile

epoch = max(315532800, int(sys.argv[1]))
stamp = __import__('time').gmtime(epoch)[:6]
for wheel in Path('dist').glob('*.whl'):
    entries = []
    with zipfile.ZipFile(wheel) as source:
        for info in sorted(source.infolist(), key=lambda item: item.filename):
            entries.append((info.filename, source.read(info.filename), info.external_attr))
    temporary = wheel.with_suffix('.normalized')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data, attrs in entries:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = attrs
            target.writestr(info, data)
    os.replace(temporary, wheel)
PY
(
  cd dist
  shasum -a 256 hermes_omp-* > SHA256SUMS
)
