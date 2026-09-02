from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_omp.core import Paths, Session, SessionStore
from hermes_omp.logging import (
    DEFAULT_BACKUPS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_RETENTION_DAYS,
    LogConfig,
    StructuredLog,
    iter_log_records,
)


def test_log_defaults_and_overrides_are_finite_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_OMP_LOG_MAX_BYTES", raising=False)
    config = LogConfig.from_env()
    assert (config.max_bytes, config.backups, config.retention_days, config.max_record_bytes) == (
        DEFAULT_MAX_BYTES, DEFAULT_BACKUPS, DEFAULT_RETENTION_DAYS, DEFAULT_MAX_RECORD_BYTES
    )
    monkeypatch.setenv("HERMES_OMP_LOG_MAX_BYTES", "nan")
    with pytest.raises(ValueError):
        LogConfig.from_env()
    monkeypatch.setenv("HERMES_OMP_LOG_MAX_BYTES", "0")
    with pytest.raises(ValueError):
        LogConfig.from_env()


def test_structured_log_rotates_bounds_records_redacts_and_filters(tmp_path: Path) -> None:
    path = tmp_path / "demo.jsonl"
    log = StructuredLog(path, LogConfig(max_bytes=500, backups=2, retention_days=14, max_record_bytes=180))
    assert not log.write({"type": "stream_event", "delta": "drop"})
    assert not log.write({"type": "tool_delta", "content": "drop"})
    sentinel = "SECRET-SENTINEL"
    for index in range(20):
        assert log.write({"type": "turn_end", "timestamp": index, "token": sentinel, "content": "x" * 400})
    files = [path, path.with_name(path.name + ".1"), path.with_name(path.name + ".2")]
    assert all(item.stat().st_mode & 0o077 == 0 for item in files if item.exists())
    assert sum(item.stat().st_size for item in files if item.exists()) <= 3 * 500
    raw = b"".join(item.read_bytes() for item in files if item.exists())
    assert sentinel.encode() not in raw
    assert b"[REDACTED]" in raw and b'"truncated":true' in raw
    assert all(len(line) <= 180 for line in raw.splitlines())


def test_cross_process_writers_leave_valid_bounded_ndjson(tmp_path: Path) -> None:
    path = tmp_path / "shared.jsonl"
    script = """import json,sys
from pathlib import Path
from hermes_omp.logging import LogConfig, StructuredLog
log=StructuredLog(Path(sys.argv[1]), LogConfig(max_bytes=2048,backups=3,retention_days=14,max_record_bytes=256))
for i in range(100): log.write({'type':'turn_end','writer':sys.argv[2],'i':i,'content':'x'*50})
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path), str(i)], env=env) for i in range(4)]
    assert [process.wait(timeout=20) for process in processes] == [0, 0, 0, 0]
    records = list(iter_log_records(path))
    assert records and all(isinstance(record, dict) for record in records)
    assert len(list(tmp_path.glob("shared.jsonl*"))) <= 5  # current + lock + three backups


def test_iter_log_records_reads_oldest_to_current_across_rotation(tmp_path: Path) -> None:
    path = tmp_path / "demo.jsonl"
    path.with_name("demo.jsonl.2").write_text('{"i":1}\n')
    path.with_name("demo.jsonl.1").write_text('{"i":2}\n')
    path.write_text('{"i":3}\n')
    assert [value["i"] for value in iter_log_records(path)] == [1, 2, 3]


def test_doctor_detects_oversized_log_and_refuses_live_remediation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_omp.cli import doctor
    paths = Paths(tmp_path / "omp")
    paths.ensure()
    path = paths.logs / "demo.jsonl"
    path.write_bytes(b"x" * 101)
    session = Session.new(name="demo", cwd=str(tmp_path), model="m", mission="x")
    SessionStore(paths).save(session)
    monkeypatch.setenv("HERMES_OMP_LOG_MAX_BYTES", "100")
    monkeypatch.setenv("HERMES_OMP_BINARY", "/bin/true")
    monkeypatch.setenv("HERMES_OMP_HERMES", "/bin/true")
    (paths.run / "demo.owner").write_text(json.dumps({"pid": os.getpid(), "session_id": session.id, "token": "x"}))
    report = doctor(paths, fix=True)
    repair = next(item for item in report["repairs"] if item["action"] == "rotate_oversized_log")
    assert repair["applied"] is False and repair["reason"] == "live_writer"
    assert path.stat().st_size == 101


def test_remove_retains_logs_unless_explicitly_purged(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_omp.cli import main
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    argv = ["create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "x", "--omp-path", "/bin/true", "--no-install"]
    assert main(argv) == 0
    paths = Paths.discover()
    (paths.logs / "demo.jsonl").write_text("{}\n")
    assert main(["remove", "demo", "--no-service"]) == 0
    assert (paths.logs / "demo.jsonl").exists()
    assert main(["create", "demo", "--cwd", str(tmp_path), "--model", "m", "--mission", "x", "--omp-path", "/bin/true", "--no-install"]) == 0
    assert main(["remove", "demo", "--no-service", "--purge-logs"]) == 0
    assert not list(paths.logs.glob("demo.jsonl*"))
