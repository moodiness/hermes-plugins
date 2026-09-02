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
(
  cd dist
  shasum -a 256 hermes_omp-* > SHA256SUMS
)
